use crate::daemon::{self, DaemonConfig};
use crate::protocol::{PruneResult, wire_name};
use crate::runtime::Runtime;
use crate::ui;
use clap::{Args, Parser, Subcommand};
use serde_json::json;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::Duration;

#[derive(Parser)]
#[command(
    name = "needle",
    version,
    about = "Needle: local pruning runtime for Pi"
)]
pub struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    /// Run the setup wizard (also runs on a bare `needle` when unconfigured).
    Setup(SetupArgs),
    /// Print Needle-owned local paths.
    Paths(PathsArgs),
    /// Prune text against a focus question using the local model.
    Prune(PruneArgs),
    /// Run the Needle daemon in the foreground.
    Daemon(DaemonArgs),
    /// Report daemon mode and backend status.
    Status(StatusArgs),
    /// Remove Pi integration and Needle-owned runtime state.
    Uninstall(UninstallArgs),
}

#[derive(Args)]
struct SetupArgs {
    /// Print intended changes without touching anything.
    #[arg(long)]
    dry_run: bool,
    /// Answer yes to every prompt.
    #[arg(long)]
    yes: bool,
}

#[derive(Args)]
struct UninstallArgs {
    /// Also remove the private worker venv, models, and logs under NEEDLE_HOME.
    #[arg(long)]
    purge: bool,
    /// Answer yes to every prompt.
    #[arg(long)]
    yes: bool,
}

#[derive(Args)]
struct DaemonArgs {
    /// Socket path (default: $NEEDLE_SOCKET or NEEDLE_HOME/runtime/needle.sock).
    #[arg(long)]
    socket: Option<PathBuf>,
    /// Seconds a session lease survives without a heartbeat.
    #[arg(long, default_value_t = 90)]
    lease_ttl_secs: u64,
}

#[derive(Args)]
struct StatusArgs {
    /// Socket path (default: $NEEDLE_SOCKET or NEEDLE_HOME/runtime/needle.sock).
    #[arg(long)]
    socket: Option<PathBuf>,
    /// Print the raw JSON status response.
    #[arg(long)]
    json: bool,
}

#[derive(Args)]
struct PathsArgs {
    /// Print the paths as JSON.
    #[arg(long)]
    json: bool,
}

#[derive(Args)]
struct PruneArgs {
    /// Focus question describing what you need from the text.
    #[arg(long)]
    query: String,
    /// File to prune; reads stdin when omitted or "-".
    file: Option<PathBuf>,
    /// Print a JSON envelope (decision/reason/backend/stats/text) to stdout.
    #[arg(long)]
    json: bool,
}

pub fn run() -> ExitCode {
    match Cli::parse().command {
        Some(Command::Setup(args)) => run_setup(args),
        Some(Command::Paths(args)) => run_paths(args),
        Some(Command::Prune(args)) => run_prune(args),
        Some(Command::Daemon(args)) => run_daemon(args),
        Some(Command::Status(args)) => run_status(args),
        Some(Command::Uninstall(args)) => run_uninstall(args),
        None => run_bare(),
    }
}

fn run_paths(args: PathsArgs) -> ExitCode {
    let paths = daemon::resolved_paths();
    if args.json {
        match serde_json::to_string(&paths) {
            Ok(text) => println!("{text}"),
            Err(error) => {
                ui::error(format!("needle: failed to serialize paths: {error}"));
                return ExitCode::FAILURE;
            }
        }
    } else {
        println!("home: {}", paths.home.display());
        println!("socket: {}", paths.socket.display());
        println!("config: {}", paths.config.display());
    }
    ExitCode::SUCCESS
}

/// Bare `needle`: the wizard on an unconfigured machine, status otherwise.
fn run_bare() -> ExitCode {
    if crate::config::is_configured() {
        let status = run_status(StatusArgs {
            socket: None,
            json: false,
        });
        if ui::fancy() {
            ui::info("`needle setup` re-runs setup; `needle --help` lists commands");
        } else {
            println!("(`needle setup` re-runs setup; `needle --help` lists commands)");
        }
        return status;
    }
    run_setup(SetupArgs {
        dry_run: false,
        yes: false,
    })
}

fn run_setup(args: SetupArgs) -> ExitCode {
    let options = crate::setup::SetupOptions {
        dry_run: args.dry_run,
        assume_yes: args.yes,
    };
    match crate::setup::run(&options) {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::FAILURE,
        Err(error) => {
            ui::error(format!("needle: setup failed: {error}"));
            ExitCode::FAILURE
        }
    }
}

fn run_uninstall(args: UninstallArgs) -> ExitCode {
    let options = crate::uninstall::UninstallOptions {
        purge: args.purge,
        assume_yes: args.yes,
    };
    match crate::uninstall::run(&options) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            ui::error(format!("needle: uninstall failed: {error}"));
            ExitCode::FAILURE
        }
    }
}

fn run_daemon(args: DaemonArgs) -> ExitCode {
    let config = DaemonConfig {
        socket: args.socket.unwrap_or_else(daemon::default_socket_path),
        lease_ttl: Duration::from_secs(args.lease_ttl_secs),
    };
    match daemon::run(config) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            ui::error(format!("needle: daemon failed: {error}"));
            ExitCode::FAILURE
        }
    }
}

fn run_status(args: StatusArgs) -> ExitCode {
    let socket = args.socket.unwrap_or_else(daemon::default_socket_path);
    match daemon::query(&socket, &json!({"op": "status"})) {
        Ok(status) => {
            if args.json {
                println!("{status}");
            } else {
                let mode = status["mode"].as_str().unwrap_or("unknown");
                let backend = status["backend_status"].as_str().unwrap_or("unknown");
                let sessions = status["sessions"].as_u64().unwrap_or(0);
                print_status_running(mode, backend, sessions);
            }
            ExitCode::SUCCESS
        }
        Err(_) => {
            if args.json {
                println!(
                    r#"{{"ok":true,"mode":"off","backend_status":"cold","sessions":0,"daemon":"not running"}}"#
                );
            } else {
                print_status_off();
            }
            ExitCode::SUCCESS
        }
    }
}

fn run_prune(args: PruneArgs) -> ExitCode {
    let text = match read_input(args.file.as_deref()) {
        Ok(text) => text,
        Err(error) => {
            ui::error(format!("needle: failed to read input: {error}"));
            return ExitCode::FAILURE;
        }
    };

    // One-shot path: an ephemeral runtime with the same blocking-residency
    // semantics as the daemon. Enable loads the model, drop unloads it.
    let runtime = Runtime::new();
    let session = "needle-cli";
    if let Err(error) = runtime.enable(session) {
        ui::error(format!("needle: prune failed: {error}"));
        return ExitCode::FAILURE;
    }
    match runtime.prune(session, &text, &args.query) {
        Ok(result) => {
            print_result(&result, args.json);
            ExitCode::SUCCESS
        }
        Err(error) => {
            ui::error(format!("needle: prune failed: {error}"));
            ExitCode::FAILURE
        }
    }
}

fn read_input(file: Option<&Path>) -> std::io::Result<String> {
    match file {
        Some(path) if path.as_os_str() != "-" => std::fs::read_to_string(path),
        _ => {
            let mut text = String::new();
            std::io::stdin().read_to_string(&mut text)?;
            Ok(text)
        }
    }
}

fn print_result(result: &PruneResult, as_json: bool) {
    if as_json {
        let envelope = json!({
            "decision": result.decision,
            "reason": result.reason,
            "backend": result.backend,
            "stats": result.stats,
            "text": result.text,
        });
        println!("{envelope}");
        return;
    }

    print!("{}", result.text);
    if !result.text.ends_with('\n') {
        println!();
    }
    let summary = summary(result);
    if ui::fancy() {
        ui::info(format!("prune: {summary}"));
    } else {
        eprintln!("needle: {summary}");
    }
}

fn print_status_running(mode: &str, backend: &str, sessions: u64) {
    let plural = if sessions == 1 { "" } else { "s" };
    if ui::fancy() {
        ui::intro("needle status");
        ui::success(format!(
            "{mode} · backend {backend} · {sessions} session{plural}"
        ));
        ui::outro("daemon is reachable");
    } else {
        println!("needle: {mode} · backend {backend} · {sessions} session{plural}");
    }
}

fn print_status_off() {
    if ui::fancy() {
        ui::intro("needle status");
        ui::warning("off (no daemon running)");
        ui::outro("start a Pi session to load Needle");
    } else {
        println!("needle: off (no daemon running)");
    }
}

fn summary(result: &PruneResult) -> String {
    let decision = wire_name(result.decision);
    let mut parts = vec![match &result.reason {
        Some(reason) => format!("{decision} ({reason})"),
        None => decision,
    }];
    let stat = |key: &str| result.stats.get(key).and_then(serde_json::Value::as_i64);
    if let (Some(input), Some(output)) = (stat("input_chars"), stat("output_chars")) {
        parts.push(format!("{input} -> {output} chars"));
    }
    if let Some(total_ms) = result
        .stats
        .get("total_ms")
        .and_then(serde_json::Value::as_f64)
    {
        parts.push(format!("{total_ms:.0}ms"));
    }
    if let Some(backend) = &result.backend {
        parts.push(format!("backend {backend}"));
    }
    parts.join(" · ")
}
