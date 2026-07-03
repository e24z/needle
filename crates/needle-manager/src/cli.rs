use crate::daemon::{self, DaemonConfig};
use crate::protocol::{PruneDecision, PruneResult};
use crate::runtime::Runtime;
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
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Prune text against a focus question using the local model.
    Prune(PruneArgs),
    /// Run the Needle daemon in the foreground.
    Daemon(DaemonArgs),
    /// Report daemon mode and backend status.
    Status(StatusArgs),
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
        Command::Prune(args) => run_prune(args),
        Command::Daemon(args) => run_daemon(args),
        Command::Status(args) => run_status(args),
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
            eprintln!("needle: daemon failed: {error}");
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
                let plural = if sessions == 1 { "" } else { "s" };
                println!("needle: {mode} · backend {backend} · {sessions} session{plural}");
            }
            ExitCode::SUCCESS
        }
        Err(_) => {
            if args.json {
                println!(
                    r#"{{"ok":true,"mode":"off","backend_status":"cold","sessions":0,"daemon":"not running"}}"#
                );
            } else {
                println!("needle: off (no daemon running)");
            }
            ExitCode::SUCCESS
        }
    }
}

fn run_prune(args: PruneArgs) -> ExitCode {
    let text = match read_input(args.file.as_deref()) {
        Ok(text) => text,
        Err(error) => {
            eprintln!("needle: failed to read input: {error}");
            return ExitCode::FAILURE;
        }
    };

    // One-shot path: an ephemeral runtime with the same blocking-residency
    // semantics as the daemon. Enable loads the model, drop unloads it.
    let runtime = Runtime::new();
    let session = "needle-cli";
    if let Err(error) = runtime.enable(session) {
        eprintln!("needle: prune failed: {error}");
        return ExitCode::FAILURE;
    }
    match runtime.prune(session, &text, &args.query) {
        Ok(result) => {
            print_result(&result, args.json);
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("needle: prune failed: {error}");
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
            "decision": decision_str(result.decision),
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
    eprintln!("needle: {}", summary(result));
}

fn decision_str(decision: PruneDecision) -> &'static str {
    match decision {
        PruneDecision::Pruned => "pruned",
        PruneDecision::Unchanged => "unchanged",
    }
}

fn summary(result: &PruneResult) -> String {
    let mut parts = vec![match &result.reason {
        Some(reason) => format!("{} ({reason})", decision_str(result.decision)),
        None => decision_str(result.decision).to_string(),
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
