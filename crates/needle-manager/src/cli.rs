use crate::manager::Manager;
use crate::protocol::{PruneDecision, PruneResult};
use clap::{Args, Parser, Subcommand};
use serde_json::json;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

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

    let mut manager = Manager::new();
    match manager.prune(&text, &args.query) {
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
