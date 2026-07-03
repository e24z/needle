//! The setup wizard: what a bare `needle` runs on an unconfigured machine.
//!
//! Owns product setup end to end: system check, Pi check, private worker
//! venv, model download, Pi integration, final status. Everything lands
//! under NEEDLE_HOME. Homebrew (or any installer) only places the binary;
//! this wizard is the one path that configures the product.
//!
//! Every step is idempotent: it inspects state, reports "already", and only
//! then mutates. `--dry-run` prints the mutations it would make and touches
//! nothing. Writing to the user's real Pi config happens in exactly one
//! step, behind an explicit confirmation.

use crate::config::{self, Config};
use crate::daemon::needle_home;
use std::ffi::{OsStr, OsString};
use std::io::{self, BufRead, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
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

    println!("needle setup");
    println!("  home: {}", home.display());
    if options.dry_run {
        println!("  dry run: no changes will be made");
    }
    println!();

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

    println!();
    step_final_status(&home, &config, options, ok);
    Ok(ok)
}

// --- steps -------------------------------------------------------------------

fn step_system_check() {
    println!("[1/5] system check");
    let os = std::env::consts::OS;
    let arch = std::env::consts::ARCH;
    println!("  os/arch: {os}/{arch}");
    if os != "macos" || arch != "aarch64" {
        println!(
            "  warning: the MLX backend needs Apple Silicon macOS; other platforms are untested"
        );
    }
    let python = python3();
    match probe(&python, &["--version"]) {
        Some(version) => println!("  python3: {version} ({})", python.to_string_lossy()),
        None => println!("  warning: python3 not found — the worker venv step will fail"),
    }
    println!();
}

fn step_pi_check() -> Option<String> {
    println!("[2/5] pi check");
    let version = probe(&pi_binary(), &["--version"]);
    match &version {
        Some(version) => println!("  pi: {version}"),
        None => println!("  pi not found on PATH — Pi integration will be skipped"),
    }
    println!();
    version
}

fn step_worker_env(home: &Path, config: &mut Config, options: &SetupOptions) -> io::Result<bool> {
    println!("[3/5] private worker environment");
    let venv = home.join("python").join("venv");
    let venv_python = venv.join("bin").join("python");

    if worker_env_ready(&venv_python) {
        println!("  already provisioned: {}", venv_python.display());
        config.worker_python = Some(venv_python);
        println!();
        return Ok(true);
    }

    let Some(source) = worker_source() else {
        println!(
            "  error: no worker source found (set NEEDLE_DEV_WORKER_SOURCE, or run from a checkout)"
        );
        println!();
        return Ok(false);
    };
    println!("  worker source: {}", source.display());
    println!("  will create venv: {}", venv.display());
    if options.dry_run {
        println!("  dry run: would run `python3 -m venv` and `pip install`");
        println!();
        return Ok(true);
    }
    if !confirm("  create the venv and install the worker?", options) {
        println!("  skipped");
        println!();
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
        println!("  error: venv exists but needle_worker did not import cleanly");
        println!();
        return Ok(false);
    }
    println!("  provisioned: {}", venv_python.display());
    config.worker_python = Some(venv_python);
    println!();
    Ok(true)
}

fn step_model(home: &Path, config: &mut Config, options: &SetupOptions) -> io::Result<bool> {
    println!("[4/5] model");

    // An explicit NEEDLE_MODEL_DIR (an already-downloaded snapshot) is
    // recorded and reused — model downloads are expensive.
    if let Some(dir) = std::env::var_os("NEEDLE_MODEL_DIR").map(PathBuf::from) {
        if dir.is_dir() {
            println!("  using NEEDLE_MODEL_DIR: {}", dir.display());
            config.model_dir = Some(dir);
            println!();
            return Ok(true);
        }
        println!("  warning: NEEDLE_MODEL_DIR is set but not a directory; ignoring");
    }

    if let Some(dir) = &config.model_dir {
        if dir.join("needle-model.json").exists() || dir.join("config.json").exists() {
            println!("  already present: {}", dir.display());
            println!();
            return Ok(true);
        }
    }

    let Some(worker_python) = config.worker_python.clone() else {
        println!("  skipped: worker environment is not provisioned");
        println!();
        return Ok(false);
    };

    println!(
        "  will download the model (~1.5 GB) into {}",
        home.join("models").display()
    );
    if options.dry_run {
        println!("  dry run: would run `python -m needle_worker.model_download_cli`");
        println!();
        return Ok(true);
    }
    if !confirm("  download now?", options) {
        println!("  skipped");
        println!();
        return Ok(false);
    }

    let output = Command::new(&worker_python)
        .args(["-m", "needle_worker.model_download_cli"])
        .env("NEEDLE_HOME", home)
        .output()?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let response: serde_json::Value =
        serde_json::from_str(stdout.trim().lines().last().unwrap_or(""))
            .unwrap_or_else(|_| serde_json::json!({"ok": false, "error": stdout.trim()}));
    if response["ok"] != true {
        println!("  error: model download failed: {}", response["error"]);
        println!();
        return Ok(false);
    }
    let path = PathBuf::from(response["path"].as_str().unwrap_or_default());
    println!(
        "  {}: {}",
        if response["downloaded"] == true {
            "downloaded"
        } else {
            "already present"
        },
        path.display()
    );
    config.model_dir = Some(path);
    println!();
    Ok(true)
}

fn step_pi_integration(
    home: &Path,
    config: &mut Config,
    options: &SetupOptions,
    pi: Option<&str>,
) -> io::Result<bool> {
    println!("[5/5] pi integration");
    if pi.is_none() {
        println!("  skipped: pi is not installed");
        println!();
        return Ok(true);
    }
    if config.pi_integrated {
        println!("  already registered with pi");
        println!();
        return Ok(true);
    }

    let Some(source) = pi_package_source() else {
        println!(
            "  error: pi package source not found (set NEEDLE_DEV_PI_PACKAGE, or run from a checkout)"
        );
        println!();
        return Ok(false);
    };
    let target = home.join("pi");
    println!("  package source: {}", source.display());
    println!(
        "  will copy to {} and run `pi install {}`",
        target.display(),
        target.display()
    );
    println!("  note: `pi install` modifies your Pi settings (~/.pi)");
    if options.dry_run {
        println!("  dry run: would copy the package and run `pi install`");
        println!();
        return Ok(true);
    }
    if !confirm("  register Needle with Pi?", options) {
        println!("  skipped — run `needle setup` again when ready");
        println!();
        return Ok(true);
    }

    copy_dir(&source, &target)?;
    run_logged(
        Command::new(pi_binary()).arg("install").arg(&target),
        "pi install",
    )?;
    config.pi_integrated = true;
    println!("  registered: {}", target.display());
    println!();
    Ok(true)
}

fn step_final_status(home: &Path, config: &Config, options: &SetupOptions, ok: bool) {
    if options.dry_run {
        println!("dry run complete: no changes made");
        return;
    }
    if ok {
        println!("needle is set up.");
    } else {
        println!("setup finished with skipped or failed steps — run `needle setup` again.");
    }
    println!("  config: {}", config::config_path().display());
    if let Some(python) = &config.worker_python {
        println!("  worker: {}", python.display());
    }
    if let Some(model) = &config.model_dir {
        println!("  model:  {}", model.display());
    }
    println!(
        "  socket: {}",
        home.join("runtime").join("needle.sock").display()
    );
    println!();
    println!("Open a Pi session to use Needle, or check `needle status`.");
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

fn confirm(prompt: &str, options: &SetupOptions) -> bool {
    if options.assume_yes {
        println!("{prompt} yes (--yes)");
        return true;
    }
    print!("{prompt} [y/N] ");
    let _ = io::stdout().flush();
    let mut answer = String::new();
    if io::stdin().lock().read_line(&mut answer).is_err() {
        return false;
    }
    matches!(answer.trim().to_lowercase().as_str(), "y" | "yes")
}

fn run_logged(command: &mut Command, what: &str) -> io::Result<()> {
    let output = command.output()?;
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
