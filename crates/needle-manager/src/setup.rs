//! The setup wizard: what a bare `needle` runs on an unconfigured machine.
//!
//! Owns product setup: system check, Pi check, private worker venv, model
//! download, Pi integration, final status. Everything lands
//! under NEEDLE_HOME. Homebrew (or any installer) only places the binary;
//! this wizard is the one path that configures the product.
//!
//! Every step is idempotent: it inspects state, reports "already", and only
//! then mutates. `--dry-run` prints the mutations it would make and touches
//! nothing. The Pi config write happens in one step, behind confirmation.

use crate::config::{self, Config};
use crate::daemon::needle_home;
use crate::ui;
use std::ffi::{OsStr, OsString};
use std::io;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::sync::OnceLock;

pub struct SetupOptions {
    pub dry_run: bool,
    /// Answer yes to every prompt (still honors dry_run).
    pub assume_yes: bool,
}

pub fn run(options: &SetupOptions) -> io::Result<bool> {
    let home = needle_home();
    let mut config = config::load().unwrap_or_default();
    let mut ok = true;

    ui::intro("needle setup");
    ui::info(format!("home: {}", home.display()));
    if options.dry_run {
        ui::warning("dry run: no changes will be made");
    }

    step_system_check();
    let pi = step_pi_check();
    ok &= step_worker_env(&home, &mut config, options)?;
    ok &= step_model(&home, &mut config, options)?;
    ok &= step_pi_integration(&home, &mut config, options, pi.as_deref())?;

    if !options.dry_run {
        config.created_at.get_or_insert_with(now_iso8601);
        config.needle_version = Some(env!("CARGO_PKG_VERSION").to_string());
        config::save(&config)?;
        std::fs::create_dir_all(home.join("logs"))?;
    }

    step_final_status(&home, &config, options, ok);
    Ok(ok)
}

// --- steps -------------------------------------------------------------------

fn step_system_check() {
    ui::step(1, 5, "system check");
    let os = std::env::consts::OS;
    let arch = std::env::consts::ARCH;
    ui::info(format!("os/arch: {os}/{arch}"));
    if os != "macos" || arch != "aarch64" {
        ui::warning("the MLX backend needs Apple Silicon macOS; other platforms are untested");
    }
    let python = python3();
    match probe(&python, &["--version"]) {
        Some(version) => ui::info(format!("python3: {version} ({})", python.to_string_lossy())),
        None => ui::warning("python3 not found; the worker venv step will fail"),
    }
}

fn step_pi_check() -> Option<String> {
    ui::step(2, 5, "pi check");
    let version = probe(&pi_binary(), &["--version"]);
    match &version {
        Some(version) => ui::info(format!("pi: {version}")),
        None => ui::warning("pi not found on PATH; Pi integration will be skipped"),
    }
    version
}

fn step_worker_env(home: &Path, config: &mut Config, options: &SetupOptions) -> io::Result<bool> {
    ui::step(3, 5, "private worker environment");
    let venv = home.join("python").join("venv");
    let venv_python = venv.join("bin").join("python");

    if worker_env_ready(&venv_python) {
        ui::success(format!("already provisioned: {}", venv_python.display()));
        config.worker_python = Some(venv_python);
        return Ok(true);
    }

    let Some(source) = worker_source() else {
        ui::error(
            "error: no worker source found (set NEEDLE_DEV_WORKER_SOURCE, or run from a checkout)",
        );
        return Ok(false);
    };
    ui::info(format!("worker source: {}", source.display()));
    ui::info(format!("will create venv: {}", venv.display()));
    if options.dry_run {
        ui::info("dry run: would run `python3 -m venv` and `pip install`");
        config.worker_python = Some(venv_python);
        return Ok(true);
    }
    if !ui::confirm(
        "Create the venv and install the worker?",
        options.assume_yes,
    ) {
        ui::warning("skipped");
        return Ok(false);
    }

    run_logged(
        Command::new(python3()).args(["-m", "venv"]).arg(&venv),
        "create venv",
    )?;
    run_logged(
        Command::new(venv.join("bin").join("pip"))
            .args(["install", "--quiet"])
            .arg(&source),
        "install worker package",
    )?;
    if !worker_env_ready(&venv_python) {
        ui::error("error: venv exists but needle_worker did not import cleanly");
        return Ok(false);
    }
    ui::success(format!("provisioned: {}", venv_python.display()));
    config.worker_python = Some(venv_python);
    Ok(true)
}

fn step_model(home: &Path, config: &mut Config, options: &SetupOptions) -> io::Result<bool> {
    ui::step(4, 5, "model");

    // An explicit NEEDLE_MODEL_DIR (an already-downloaded snapshot) is
    // recorded and reused because model downloads are expensive.
    if let Some(dir) = std::env::var_os("NEEDLE_MODEL_DIR").map(PathBuf::from) {
        if dir.is_dir() {
            ui::success(format!("using NEEDLE_MODEL_DIR: {}", dir.display()));
            config.model_dir = Some(dir);
            return Ok(true);
        }
        ui::warning("NEEDLE_MODEL_DIR is set but not a directory; ignoring");
    }

    if let Some(dir) = &config.model_dir {
        if dir.join("needle-model.json").exists() || dir.join("config.json").exists() {
            ui::success(format!("already present: {}", dir.display()));
            return Ok(true);
        }
    }

    let Some(worker_python) = config.worker_python.clone() else {
        ui::warning("skipped: worker environment is not provisioned");
        return Ok(false);
    };

    ui::info(format!(
        "will download the model (~1.5 GB) into {}",
        home.join("models").display()
    ));
    if options.dry_run {
        ui::info("dry run: would run `python -m needle_worker.model_download_cli`");
        return Ok(true);
    }
    if !ui::confirm("Download the model now?", options.assume_yes) {
        ui::warning("skipped");
        return Ok(false);
    }

    ui::info("download progress will appear below when Hugging Face reports it");
    let output = ui::activity("downloading model", "model download finished", || {
        Command::new(&worker_python)
            .args(["-m", "needle_worker.model_download_cli"])
            .env("NEEDLE_HOME", home)
            .stderr(Stdio::inherit())
            .output()
    })?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let response: serde_json::Value =
        serde_json::from_str(stdout.trim().lines().last().unwrap_or(""))
            .unwrap_or_else(|_| serde_json::json!({"ok": false, "error": stdout.trim()}));
    if response["ok"] != true {
        ui::error(format!(
            "error: model download failed: {}",
            response["error"]
        ));
        return Ok(false);
    }
    let path = PathBuf::from(response["path"].as_str().unwrap_or_default());
    ui::success(format!(
        "{}: {}",
        if response["downloaded"] == true {
            "downloaded"
        } else {
            "already present"
        },
        path.display()
    ));
    config.model_dir = Some(path);
    Ok(true)
}

fn step_pi_integration(
    home: &Path,
    config: &mut Config,
    options: &SetupOptions,
    pi: Option<&str>,
) -> io::Result<bool> {
    ui::step(5, 5, "pi integration");
    if pi.is_none() {
        ui::warning("skipped: pi is not installed");
        return Ok(true);
    }
    let Some(source) = pi_package_source() else {
        ui::error(
            "error: pi package source not found (set NEEDLE_DEV_PI_PACKAGE, or run from a checkout)",
        );
        return Ok(false);
    };
    let target = home.join("pi");
    let registrations = match pi_registrations() {
        Ok(registrations) => registrations,
        Err(error) => {
            ui::warning(format!("could not inspect existing Pi packages: {error}"));
            Vec::new()
        }
    };
    let current_registered = registrations
        .iter()
        .any(|registration| registration.matches_path(&target));
    let stale = registrations
        .iter()
        .filter(|registration| {
            !registration.matches_path(&target) && registration.looks_like_needle_package()
        })
        .collect::<Vec<_>>();

    if config.pi_integrated && current_registered && stale.is_empty() && target.exists() {
        ui::success("already registered with pi");
        return Ok(true);
    }

    ui::info(format!("package source: {}", source.display()));
    for registration in &stale {
        ui::warning(format!(
            "will remove stale Needle Pi package: {}",
            registration.source
        ));
    }
    ui::info(format!(
        "will copy to {} and run `pi install {}`",
        target.display(),
        target.display()
    ));
    ui::warning("`pi install` modifies your Pi settings (~/.pi)");
    if options.dry_run {
        ui::info("dry run: would copy the package and run `pi install`");
        return Ok(true);
    }
    if !ui::confirm("Register Needle with Pi?", options.assume_yes) {
        ui::warning("skipped; run `needle setup` again when ready");
        return Ok(true);
    }

    for registration in stale {
        run_logged(
            Command::new(pi_binary())
                .arg("uninstall")
                .arg(&registration.source),
            &format!("pi uninstall {}", registration.source),
        )?;
    }
    copy_dir(&source, &target)?;
    run_logged(
        Command::new(pi_binary()).arg("install").arg(&target),
        "pi install",
    )?;
    config.pi_integrated = true;
    ui::success(format!("registered: {}", target.display()));
    Ok(true)
}

fn step_final_status(home: &Path, config: &Config, options: &SetupOptions, ok: bool) {
    if options.dry_run {
        ui::outro("dry run complete: no changes made");
        return;
    }
    let mut details = format!("config: {}", config::config_path().display());
    if let Some(python) = &config.worker_python {
        details.push_str(&format!("\nworker: {}", python.display()));
    }
    if let Some(model) = &config.model_dir {
        details.push_str(&format!("\nmodel:  {}", model.display()));
    }
    details.push_str(&format!(
        "\nsocket: {}",
        home.join("runtime").join("needle.sock").display()
    ));
    ui::note("Configured paths", details);
    if ok {
        ui::outro("needle is set up. Open a Pi session to use Needle, or check `needle status`.");
    } else {
        ui::outro_cancel("setup finished with skipped or failed steps; run `needle setup` again.");
    }
}

// --- helpers -----------------------------------------------------------------

fn worker_env_ready(venv_python: &Path) -> bool {
    venv_python.exists()
        && Command::new(venv_python)
            .args(["-c", "import needle_worker"])
            .status()
            .map(|status| status.success())
            .unwrap_or(false)
}

/// The worker package to install: an explicit override, a wheel/source dir
/// shipped next to the binary, or the repo checkout during development.
fn worker_source() -> Option<PathBuf> {
    if let Some(source) = std::env::var_os("NEEDLE_DEV_WORKER_SOURCE").map(PathBuf::from) {
        return source.exists().then_some(source);
    }
    if let Some(wheel) = shipped_worker_wheel() {
        return Some(wheel);
    }
    if let Some(shipped) = sibling("share/needle/python") {
        return Some(shipped);
    }
    dev_path("python")
}

fn shipped_worker_wheel() -> Option<PathBuf> {
    let wheel_dir = sibling("share/needle/wheels")?;
    let mut wheels = std::fs::read_dir(wheel_dir)
        .ok()?
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.starts_with("needle_worker-") && name.ends_with(".whl"))
        })
        .collect::<Vec<_>>();
    wheels.sort();
    wheels.pop()
}

fn pi_package_source() -> Option<PathBuf> {
    if let Some(source) = std::env::var_os("NEEDLE_DEV_PI_PACKAGE").map(PathBuf::from) {
        return source.exists().then_some(source);
    }
    if let Some(shipped) = sibling("share/needle/pi") {
        return Some(shipped);
    }
    dev_path("pi")
}

#[derive(Debug)]
struct PiRegistration {
    source: String,
    resolved: Option<PathBuf>,
}

impl PiRegistration {
    fn matches_path(&self, path: &Path) -> bool {
        self.paths()
            .into_iter()
            .any(|candidate| paths_equivalent(&candidate, path))
    }

    fn looks_like_needle_package(&self) -> bool {
        let source = self.source.to_lowercase();
        if source.contains("needle/hosts/pi") || source.contains("site-packages/needle/hosts/pi") {
            return true;
        }
        self.paths().into_iter().any(|path| {
            path.join("extension.js").exists() && package_json_is_needle_pi_package(&path)
        })
    }

    fn paths(&self) -> Vec<PathBuf> {
        let mut paths = Vec::new();
        paths.push(expand_tilde(&self.source));
        if let Some(path) = &self.resolved {
            paths.push(path.clone());
        }
        paths
    }
}

fn package_json_is_needle_pi_package(path: &Path) -> bool {
    let Ok(text) = std::fs::read_to_string(path.join("package.json")) else {
        return false;
    };
    let Ok(package) = serde_json::from_str::<serde_json::Value>(&text) else {
        return false;
    };
    package.get("name").and_then(|name| name.as_str()) == Some("needle")
        && package
            .get("pi")
            .and_then(|pi| pi.get("extensions"))
            .and_then(|extensions| extensions.as_array())
            .is_some_and(|extensions| !extensions.is_empty())
}

fn pi_registrations() -> io::Result<Vec<PiRegistration>> {
    let output = Command::new(pi_binary()).arg("list").output()?;
    if !output.status.success() {
        return Err(io::Error::other(format!(
            "pi list failed with {}",
            output.status
        )));
    }
    Ok(parse_pi_list(&String::from_utf8_lossy(&output.stdout)))
}

fn parse_pi_list(output: &str) -> Vec<PiRegistration> {
    let mut registrations = Vec::new();
    for line in output.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.ends_with("packages:") {
            continue;
        }
        let indent = line.len() - line.trim_start().len();
        if indent <= 2 {
            registrations.push(PiRegistration {
                source: trimmed.to_string(),
                resolved: None,
            });
        } else if let Some(registration) = registrations.last_mut() {
            registration
                .resolved
                .get_or_insert_with(|| PathBuf::from(trimmed));
        }
    }
    registrations
}

fn expand_tilde(path: &str) -> PathBuf {
    if let Some(rest) = path.strip_prefix("~/") {
        if let Some(home) = std::env::var_os("HOME") {
            return PathBuf::from(home).join(rest);
        }
    }
    PathBuf::from(path)
}

fn paths_equivalent(candidate: &Path, expected: &Path) -> bool {
    if candidate == expected {
        return true;
    }
    match (candidate.canonicalize(), expected.canonicalize()) {
        (Ok(candidate), Ok(expected)) => candidate == expected,
        _ => false,
    }
}

fn sibling(relative: &str) -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let prefix = exe.parent()?.parent()?;
    let path = prefix.join(relative);
    path.is_dir().then_some(path)
}

fn dev_path(relative: &str) -> Option<PathBuf> {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()?
        .parent()?
        .join(relative);
    path.is_dir().then_some(path)
}

/// First output line of `command args`, or None if it cannot run.
fn probe(command: &OsStr, args: &[&str]) -> Option<String> {
    let output = Command::new(command).args(args).output().ok()?;
    if !output.status.success() {
        return None;
    }
    let text = if output.stdout.is_empty() {
        String::from_utf8_lossy(&output.stderr).into_owned()
    } else {
        String::from_utf8_lossy(&output.stdout).into_owned()
    };
    text.lines().next().map(|line| line.trim().to_string())
}

fn python3() -> OsString {
    static PYTHON3: OnceLock<OsString> = OnceLock::new();
    PYTHON3.get_or_init(select_python3).clone()
}

fn select_python3() -> OsString {
    if let Some(python) = std::env::var_os("NEEDLE_DEV_SETUP_PYTHON") {
        return python;
    }

    for candidate in setup_python_candidates() {
        if python_can_create_venv_with_pip(&candidate) {
            return candidate;
        }
    }
    "python3".into()
}

fn setup_python_candidates() -> Vec<OsString> {
    let mut candidates = Vec::new();
    push_candidate(&mut candidates, "python3.13");
    push_candidate(&mut candidates, "/opt/homebrew/bin/python3.13");
    push_candidate(
        &mut candidates,
        "/opt/homebrew/opt/python@3.13/bin/python3.13",
    );
    push_candidate(&mut candidates, "/usr/local/bin/python3.13");
    push_candidate(&mut candidates, "python3");
    candidates
}

fn push_candidate(candidates: &mut Vec<OsString>, candidate: impl Into<OsString>) {
    let candidate = candidate.into();
    if !candidates.iter().any(|existing| existing == &candidate) {
        candidates.push(candidate);
    }
}

fn python_can_create_venv_with_pip(command: &OsStr) -> bool {
    if !python_is_at_least(command, 3, 13) {
        return false;
    }

    let probe_root = std::env::temp_dir().join(format!(
        "needle-python-probe-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|duration| duration.as_nanos())
            .unwrap_or(0)
    ));
    if std::fs::create_dir_all(&probe_root).is_err() {
        return false;
    }

    let venv = probe_root.join("venv");
    let created = Command::new(command)
        .args(["-m", "venv"])
        .arg(&venv)
        .output()
        .map(|output| output.status.success())
        .unwrap_or(false);
    let pip_ready = created
        && Command::new(venv.join("bin").join("python"))
            .args(["-m", "pip", "--version"])
            .output()
            .map(|output| output.status.success())
            .unwrap_or(false);

    let _ = std::fs::remove_dir_all(probe_root);
    pip_ready
}

fn python_is_at_least(command: &OsStr, major: u32, minor: u32) -> bool {
    let Some(version) = probe(
        command,
        &[
            "-c",
            "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
        ],
    ) else {
        return false;
    };
    let Some((found_major, found_minor)) = version.split_once('.') else {
        return false;
    };
    let Ok(found_major) = found_major.parse::<u32>() else {
        return false;
    };
    let Ok(found_minor) = found_minor.parse::<u32>() else {
        return false;
    };
    (found_major, found_minor) >= (major, minor)
}

fn pi_binary() -> OsString {
    std::env::var_os("NEEDLE_DEV_PI_BIN").unwrap_or_else(|| "pi".into())
}

fn run_logged(command: &mut Command, what: &str) -> io::Result<()> {
    let output = ui::activity(what, format!("{what}: done"), || command.output())?;
    if !output.status.success() {
        return Err(io::Error::other(format_command_failure(what, &output)));
    }
    Ok(())
}

fn format_command_failure(what: &str, output: &Output) -> String {
    let mut message = format!("{what} failed with {}", output.status);
    append_output_section(&mut message, "stdout", &output.stdout);
    append_output_section(&mut message, "stderr", &output.stderr);
    message
}

fn append_output_section(message: &mut String, label: &str, bytes: &[u8]) {
    let text = String::from_utf8_lossy(bytes);
    let text = text.trim();
    if !text.is_empty() {
        message.push_str(&format!("\n{label}:\n{text}"));
    }
}

fn copy_dir(source: &Path, target: &Path) -> io::Result<()> {
    std::fs::create_dir_all(target)?;
    for entry in std::fs::read_dir(source)? {
        let entry = entry?;
        let destination = target.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            copy_dir(&entry.path(), &destination)?;
        } else {
            std::fs::copy(entry.path(), &destination)?;
        }
    }
    Ok(())
}

fn now_iso8601() -> String {
    // Seconds since epoch is enough provenance without a chrono dependency.
    let seconds = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0);
    format!("unix:{seconds}")
}
