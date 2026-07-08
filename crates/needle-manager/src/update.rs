//! Self-update support for the release tarball distribution.
//!
//! Updating is intentionally narrow: replace the product payload under the
//! install prefix, then let `needle setup` refresh host registrations and the
//! worker environment. Package-manager-owned installs should update through
//! their package manager.

use crate::ui;
use std::io;
use std::os::unix::fs::PermissionsExt;
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Output, Stdio};

const REPO: &str = "e24z/needle";
const INSTALL_DIR_NAME: &str = "needle";

pub struct UpdateOptions {
    pub version: String,
    pub prefix: Option<PathBuf>,
    pub archive_url: Option<String>,
    pub dry_run: bool,
    /// Answer yes to update prompts. When setup runs, this is forwarded to
    /// `needle setup --yes`.
    pub assume_yes: bool,
    pub run_setup: bool,
}

pub fn run(options: &UpdateOptions) -> io::Result<bool> {
    ui::intro("needle update");

    let archive_url = archive_url(options)?;
    let prefix = match &options.prefix {
        Some(prefix) => prefix.clone(),
        None => inferred_prefix()?,
    };
    let installed_needle = prefix.join("bin").join("needle");

    ui::info(format!("current: needle {}", env!("CARGO_PKG_VERSION")));
    ui::info(format!("artifact: {archive_url}"));
    ui::info(format!("prefix: {}", prefix.display()));
    ui::info(format!("target: {}", installed_needle.display()));

    if looks_homebrew_managed(&prefix) && options.prefix.is_none() {
        return Err(io::Error::other(
            "this looks like a Homebrew-managed install; update it with Homebrew, \
             or pass --prefix to explicitly choose a tarball-managed prefix",
        ));
    }

    if options.dry_run {
        ui::warning("dry run: no changes will be made");
        ui::info("dry run: would download and unpack the release artifact");
        ui::info("dry run: would replace bin/needle and share/needle");
        if options.run_setup {
            ui::info(format!(
                "dry run: would run `{}` setup",
                installed_needle.display()
            ));
        }
        ui::outro("dry run complete: no changes made");
        return Ok(true);
    }

    if !ui::confirm(
        "Install this Needle release into the target prefix?",
        options.assume_yes,
    ) {
        ui::outro_cancel("update cancelled.");
        return Ok(false);
    }

    let tmp = temp_dir("needle-update")?;
    let result = install_from_archive(&archive_url, &prefix, &tmp);
    let cleanup_result = std::fs::remove_dir_all(&tmp);
    result?;
    if let Err(error) = cleanup_result {
        ui::warning(format!(
            "could not remove temp dir {}: {error}",
            tmp.display()
        ));
    }

    ui::success(format!("installed: {}", installed_needle.display()));

    if options.run_setup {
        run_setup(&installed_needle, options.assume_yes)?;
    } else {
        ui::info(format!("next: {} setup", installed_needle.display()));
    }

    ui::outro("needle update complete.");
    Ok(true)
}

fn archive_url(options: &UpdateOptions) -> io::Result<String> {
    if let Some(url) = &options.archive_url {
        return Ok(url.clone());
    }

    let host = host_triple()?;
    let asset = format!("needle-{host}.tar.gz");
    if options.version == "latest" {
        Ok(format!(
            "https://github.com/{REPO}/releases/latest/download/{asset}"
        ))
    } else {
        Ok(format!(
            "https://github.com/{REPO}/releases/download/{}/{asset}",
            options.version
        ))
    }
}

fn host_triple() -> io::Result<&'static str> {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("macos", "aarch64") => Ok("aarch64-apple-darwin"),
        (os, arch) => Err(io::Error::other(format!(
            "Needle currently ships an Apple Silicon macOS artifact only; unsupported host: {os}/{arch}"
        ))),
    }
}

fn inferred_prefix() -> io::Result<PathBuf> {
    let exe = std::env::current_exe()?;
    let bin = exe.parent().ok_or_else(|| {
        io::Error::other(format!(
            "could not infer install prefix from {}",
            exe.display()
        ))
    })?;
    if bin.file_name().and_then(|name| name.to_str()) != Some("bin") {
        return Err(io::Error::other(format!(
            "could not infer install prefix from {}; pass --prefix DIR",
            exe.display()
        )));
    }
    let prefix = bin.parent().ok_or_else(|| {
        io::Error::other(format!(
            "could not infer install prefix from {}",
            exe.display()
        ))
    })?;
    if !prefix.join("share").join(INSTALL_DIR_NAME).is_dir() {
        return Err(io::Error::other(format!(
            "{} does not look like a Needle release install; pass --prefix DIR",
            prefix.display()
        )));
    }
    Ok(prefix.to_path_buf())
}

fn looks_homebrew_managed(prefix: &Path) -> bool {
    let text = prefix.to_string_lossy();
    text.contains("/Cellar/needle/")
}

fn install_from_archive(archive_url: &str, prefix: &Path, tmp: &Path) -> io::Result<()> {
    let archive = tmp.join("needle.tar.gz");
    let extract_dir = tmp.join("extract");
    std::fs::create_dir_all(&extract_dir)?;

    fetch_archive(archive_url, &archive)?;
    validate_archive(&archive)?;
    extract_archive(&archive, &extract_dir)?;
    let root = find_release_root(&extract_dir)?;

    let bin = root.join("bin").join("needle");
    let share = root.join("share").join(INSTALL_DIR_NAME);
    if !bin.is_file() {
        return Err(io::Error::other(
            "release artifact did not contain bin/needle",
        ));
    }
    if !share.is_dir() {
        return Err(io::Error::other(
            "release artifact did not contain share/needle",
        ));
    }

    install_payload(&bin, &share, prefix)?;
    Ok(())
}

fn fetch_archive(source: &str, destination: &Path) -> io::Result<()> {
    if let Some(path) = source.strip_prefix("file://") {
        std::fs::copy(path, destination)?;
        return Ok(());
    }
    let source_path = Path::new(source);
    if source_path.exists() {
        std::fs::copy(source_path, destination)?;
        return Ok(());
    }

    let mut command = Command::new("curl");
    command.args(["-fsSL", source, "-o"]).arg(destination);
    run_command(&mut command, "download release artifact")
}

fn extract_archive(archive: &Path, destination: &Path) -> io::Result<()> {
    let mut command = Command::new("tar");
    command.arg("-xzf").arg(archive).arg("-C").arg(destination);
    run_command(&mut command, "unpack release artifact")
}

fn validate_archive(archive: &Path) -> io::Result<()> {
    let names = tar_output(
        Command::new("tar").arg("-tzf").arg(archive),
        "inspect release archive",
    )?;
    let verbose = tar_output(
        Command::new("tar").arg("-tvzf").arg(archive),
        "inspect release archive metadata",
    )?;

    reject_unsupported_archive_types(&verbose)?;
    validate_archive_paths(&names)
}

fn reject_unsupported_archive_types(listing: &str) -> io::Result<()> {
    for line in listing.lines().filter(|line| !line.trim().is_empty()) {
        match line.as_bytes().first().copied() {
            Some(b'-' | b'd') => {}
            Some(kind) => {
                return Err(io::Error::other(format!(
                    "release archive contains unsupported entry type {:?}: {line}",
                    kind as char
                )));
            }
            None => {}
        }
    }
    Ok(())
}

fn validate_archive_paths(names: &str) -> io::Result<()> {
    let mut root: Option<String> = None;
    let mut saw_entry = false;
    for name in names.lines().filter(|line| !line.trim().is_empty()) {
        saw_entry = true;
        let entry_root = validate_archive_path(name)?;
        match &root {
            Some(root) if root != &entry_root => {
                return Err(io::Error::other(format!(
                    "release archive contains multiple roots: {root} and {entry_root}"
                )));
            }
            Some(_) => {}
            None => root = Some(entry_root),
        }
    }

    if !saw_entry {
        return Err(io::Error::other("release archive is empty"));
    }
    Ok(())
}

fn validate_archive_path(name: &str) -> io::Result<String> {
    let path = Path::new(name);
    if path.is_absolute() {
        return Err(io::Error::other(format!(
            "release archive contains absolute path: {name}"
        )));
    }

    let mut components = path.components();
    let Some(Component::Normal(root)) = components.next() else {
        return Err(io::Error::other(format!(
            "release archive contains invalid path: {name}"
        )));
    };
    let root = root.to_string_lossy().to_string();
    if !root.starts_with("needle-") {
        return Err(io::Error::other(format!(
            "release archive root must start with needle-: {name}"
        )));
    }

    for component in components {
        if !matches!(component, Component::Normal(_)) {
            return Err(io::Error::other(format!(
                "release archive contains unsafe path: {name}"
            )));
        }
    }
    Ok(root)
}

fn tar_output(command: &mut Command, what: &str) -> io::Result<String> {
    let output = command.output()?;
    if !output.status.success() {
        return Err(io::Error::other(format_command_failure(what, &output)));
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

fn find_release_root(extract_dir: &Path) -> io::Result<PathBuf> {
    let mut candidates = std::fs::read_dir(extract_dir)?
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| {
            path.is_dir()
                && path
                    .file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(|name| name.starts_with("needle-"))
                && path.join("bin").join("needle").is_file()
        })
        .collect::<Vec<_>>();
    candidates.sort();
    candidates
        .into_iter()
        .next()
        .ok_or_else(|| io::Error::other("release artifact did not unpack to a needle-* directory"))
}

fn install_payload(binary_source: &Path, share_source: &Path, prefix: &Path) -> io::Result<()> {
    let binary_target = prefix.join("bin").join("needle");
    let share_target = prefix.join("share").join(INSTALL_DIR_NAME);
    preflight_file_target(&binary_target)?;
    preflight_dir_target(&share_target)?;

    let binary_parent = binary_target.parent().ok_or_else(|| {
        io::Error::other(format!(
            "invalid binary target: {}",
            binary_target.display()
        ))
    })?;
    let share_parent = share_target.parent().ok_or_else(|| {
        io::Error::other(format!("invalid share target: {}", share_target.display()))
    })?;
    std::fs::create_dir_all(binary_parent)?;
    std::fs::create_dir_all(share_parent)?;

    let binary_staging = unique_child(binary_parent, ".needle-bin-new");
    let share_staging = unique_child(share_parent, ".needle-share-new");
    prepare_binary(binary_source, &binary_staging)?;
    copy_dir(share_source, &share_staging)?;

    let binary_backup = unique_child(binary_parent, ".needle-bin-previous");
    let share_backup = unique_child(share_parent, ".needle-share-previous");
    let mut binary_backed_up = false;
    let mut binary_committed = false;
    let mut share_backed_up = false;
    let mut share_committed = false;

    let result = (|| -> io::Result<()> {
        if share_target.exists() {
            std::fs::rename(&share_target, &share_backup)?;
            share_backed_up = true;
        }
        std::fs::rename(&share_staging, &share_target)?;
        share_committed = true;

        if binary_target.exists() {
            std::fs::rename(&binary_target, &binary_backup)?;
            binary_backed_up = true;
        }
        std::fs::rename(&binary_staging, &binary_target)?;
        binary_committed = true;
        Ok(())
    })();

    if let Err(error) = result {
        rollback_payload(PayloadRollback {
            binary_target: &binary_target,
            binary_backup: &binary_backup,
            binary_backed_up,
            binary_committed,
            share_target: &share_target,
            share_backup: &share_backup,
            share_backed_up,
            share_committed,
            binary_staging: &binary_staging,
            share_staging: &share_staging,
        });
        return Err(error);
    }

    cleanup_path(&binary_backup);
    cleanup_path(&share_backup);
    Ok(())
}

fn preflight_file_target(target: &Path) -> io::Result<()> {
    preflight_target(target, "binary", |metadata| metadata.is_file())
}

fn preflight_dir_target(target: &Path) -> io::Result<()> {
    preflight_target(target, "share directory", |metadata| metadata.is_dir())
}

fn preflight_target(
    target: &Path,
    label: &str,
    valid_kind: impl FnOnce(&std::fs::Metadata) -> bool,
) -> io::Result<()> {
    let Ok(metadata) = std::fs::symlink_metadata(target) else {
        return Ok(());
    };
    if metadata.file_type().is_symlink() {
        return Err(io::Error::other(format!(
            "{} is a symlink; update the owning package manager or choose a non-symlink prefix",
            target.display()
        )));
    }
    if !valid_kind(&metadata) {
        return Err(io::Error::other(format!(
            "{} exists but is not a {label}",
            target.display()
        )));
    }
    Ok(())
}

fn prepare_binary(source: &Path, target: &Path) -> io::Result<()> {
    std::fs::copy(source, target)?;
    let permissions = std::fs::metadata(source)?.permissions();
    std::fs::set_permissions(target, permissions)?;
    if std::fs::metadata(target)?.permissions().mode() & 0o111 == 0 {
        let mut permissions = std::fs::metadata(target)?.permissions();
        permissions.set_mode(permissions.mode() | 0o755);
        std::fs::set_permissions(target, permissions)?;
    }
    Ok(())
}

struct PayloadRollback<'a> {
    binary_target: &'a Path,
    binary_backup: &'a Path,
    binary_backed_up: bool,
    binary_committed: bool,
    share_target: &'a Path,
    share_backup: &'a Path,
    share_backed_up: bool,
    share_committed: bool,
    binary_staging: &'a Path,
    share_staging: &'a Path,
}

fn rollback_payload(paths: PayloadRollback<'_>) {
    if paths.binary_committed {
        cleanup_path(paths.binary_target);
    }
    if paths.binary_backed_up {
        let _ = std::fs::rename(paths.binary_backup, paths.binary_target);
    }

    if paths.share_committed {
        cleanup_path(paths.share_target);
    }
    if paths.share_backed_up {
        let _ = std::fs::rename(paths.share_backup, paths.share_target);
    }

    cleanup_path(paths.binary_staging);
    cleanup_path(paths.share_staging);
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
            std::fs::set_permissions(&destination, entry.metadata()?.permissions())?;
        }
    }
    Ok(())
}

fn run_setup(installed_needle: &Path, assume_yes: bool) -> io::Result<()> {
    ui::info("running setup to refresh host registration");
    let mut command = Command::new(installed_needle);
    command.args(["setup", "--refresh-install"]);
    if assume_yes {
        command.arg("--yes");
    }
    command
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());
    let status = command.status()?;
    if !status.success() {
        return Err(io::Error::other(format!(
            "`{} setup` failed with {status}",
            installed_needle.display()
        )));
    }
    Ok(())
}

fn run_command(command: &mut Command, what: &str) -> io::Result<()> {
    let output = ui::activity(what, format!("{what}: done"), || command.output())?;
    if output.status.success() {
        return Ok(());
    }

    Err(io::Error::other(format_command_failure(what, &output)))
}

fn format_command_failure(what: &str, output: &Output) -> String {
    let mut message = format!("{what} failed with {}", output.status);
    append_output(&mut message, "stdout", &output.stdout);
    append_output(&mut message, "stderr", &output.stderr);
    message
}

fn append_output(message: &mut String, label: &str, bytes: &[u8]) {
    let text = String::from_utf8_lossy(bytes);
    let text = text.trim();
    if !text.is_empty() {
        message.push_str(&format!("\n{label}:\n{text}"));
    }
}

fn temp_dir(label: &str) -> io::Result<PathBuf> {
    let path = std::env::temp_dir().join(format!("{label}-{}-{}", std::process::id(), now_nanos()));
    std::fs::create_dir_all(&path)?;
    Ok(path)
}

fn unique_child(parent: &Path, label: &str) -> PathBuf {
    parent.join(format!("{label}-{}-{}", std::process::id(), now_nanos()))
}

fn cleanup_path(path: &Path) {
    let Ok(metadata) = std::fs::symlink_metadata(path) else {
        return;
    };
    if metadata.is_dir() && !metadata.file_type().is_symlink() {
        let _ = std::fs::remove_dir_all(path);
    } else {
        let _ = std::fs::remove_file(path);
    }
}

fn now_nanos() -> u128 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0)
}
