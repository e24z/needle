//! Uninstall Needle-owned state without guessing at user files.
//!
//! Default uninstall removes runtime/configuration state and Pi registration
//! while keeping heavyweight local data (worker venv, model snapshots, logs).
//! `--purge` removes the whole NEEDLE_HOME tree.

use crate::config::{self, Config};
use crate::daemon::{self, needle_home};
use crate::ui;
use serde_json::json;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;

pub struct UninstallOptions {
    pub purge: bool,
    pub assume_yes: bool,
}

pub fn run(options: &UninstallOptions) -> io::Result<()> {
    let home = needle_home();
    ui::intro("needle uninstall");
    ui::info(format!("home: {}", home.display()));
    if options.purge {
        ui::warning("purge: yes");
    }

    if !home.exists() && !config::is_configured() {
        ui::outro("nothing to uninstall.");
        return Ok(());
    }

    if !ui::confirm("Remove Needle from this machine?", options.assume_yes) {
        ui::outro_cancel("cancelled.");
        return Ok(());
    }

    let config = config::load().unwrap_or_default();
    stop_daemon(&home);
    remove_pi_integration(&home, &config)?;
    remove_runtime_state(&home, options.purge)?;

    let mut note = String::new();
    if !options.purge {
        note.push_str(&format!("kept models/venv/logs under {}", home.display()));
        note.push_str("\nrun `needle uninstall --purge` to remove them too");
        ui::note("Retained data", note);
    }
    ui::outro("needle uninstalled.");
    Ok(())
}

fn stop_daemon(home: &Path) {
    let socket = daemon::default_socket_path();
    match daemon::query(&socket, &json!({"op": "shutdown"})) {
        Ok(_) => ui::success("daemon: stopped"),
        Err(_) => {
            let fallback = home.join("runtime").join("needle.sock");
            if fallback.exists() {
                let _ = std::fs::remove_file(fallback);
            }
            ui::info("daemon: not running");
        }
    }
}

fn remove_pi_integration(home: &Path, config: &Config) -> io::Result<()> {
    let package = home.join("pi");
    if config.pi_integrated && package.exists() {
        run_logged(
            Command::new(pi_binary()).arg("uninstall").arg(&package),
            "pi uninstall",
        )?;
        ui::success("pi: removed registration");
    } else {
        ui::info("pi: no registered package recorded");
    }
    if package.exists() {
        std::fs::remove_dir_all(&package)?;
        ui::success(format!("pi package: removed {}", package.display()));
    }
    Ok(())
}

fn remove_runtime_state(home: &Path, purge: bool) -> io::Result<()> {
    if purge {
        if !safe_to_remove_home(home) {
            return Err(io::Error::other(format!(
                "refusing to purge suspicious NEEDLE_HOME: {}",
                home.display()
            )));
        }
        if home.exists() {
            std::fs::remove_dir_all(home)?;
        }
        ui::success(format!("state: purged {}", home.display()));
        return Ok(());
    }

    remove_file(config::config_path())?;
    remove_dir(home.join("runtime"))?;
    Ok(())
}

fn safe_to_remove_home(home: &Path) -> bool {
    home.is_absolute()
        && home.parent().is_some()
        && home.components().count() >= 3
        && home != Path::new("/")
}

fn remove_file(path: PathBuf) -> io::Result<()> {
    match std::fs::remove_file(&path) {
        Ok(()) => {
            ui::success(format!("removed: {}", path.display()));
            Ok(())
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
    }
}

fn remove_dir(path: PathBuf) -> io::Result<()> {
    match std::fs::remove_dir_all(&path) {
        Ok(()) => {
            ui::success(format!("removed: {}", path.display()));
            Ok(())
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error),
    }
}

fn pi_binary() -> std::ffi::OsString {
    std::env::var_os("NEEDLE_DEV_PI_BIN").unwrap_or_else(|| "pi".into())
}

fn run_logged(command: &mut Command, what: &str) -> io::Result<()> {
    let status = ui::activity(what, format!("{what}: done"), || command.status())?;
    if !status.success() {
        return Err(io::Error::other(format!("{what} failed with {status}")));
    }
    Ok(())
}
