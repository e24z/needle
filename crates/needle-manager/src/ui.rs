//! Shared human-facing CLI presentation.
//!
//! `cliclack` is used only when stderr is an interactive terminal. Plain
//! output remains the fallback for tests, pipes, and automation.

use std::fmt::Display;
use std::io::{self, BufRead, IsTerminal, Write};

pub(crate) fn fancy() -> bool {
    io::stderr().is_terminal() && std::env::var_os("NEEDLE_PLAIN").is_none()
}

fn can_prompt() -> bool {
    fancy() && io::stdin().is_terminal()
}

pub(crate) fn intro(title: impl Display) {
    if fancy() {
        let _ = cliclack::intro(title);
    } else {
        println!("{title}");
    }
}

pub(crate) fn outro(message: impl Display) {
    if fancy() {
        let _ = cliclack::outro(message);
    } else {
        println!("{message}");
    }
}

pub(crate) fn outro_cancel(message: impl Display) {
    if fancy() {
        let _ = cliclack::outro_cancel(message);
    } else {
        println!("{message}");
    }
}

pub(crate) fn note(title: impl Display, body: impl Display) {
    if fancy() {
        let _ = cliclack::note(title, body);
    } else {
        println!("{title}");
        for line in body.to_string().lines() {
            println!("  {line}");
        }
    }
}

pub(crate) fn step(index: usize, total: usize, title: impl Display) {
    if fancy() {
        let _ = cliclack::log::step(format!("{index}/{total} {title}"));
    } else {
        println!("[{index}/{total}] {title}");
    }
}

pub(crate) fn info(message: impl Display) {
    if fancy() {
        let _ = cliclack::log::info(message);
    } else {
        println!("  {message}");
    }
}

pub(crate) fn success(message: impl Display) {
    if fancy() {
        let _ = cliclack::log::success(message);
    } else {
        println!("  {message}");
    }
}

pub(crate) fn warning(message: impl Display) {
    if fancy() {
        let _ = cliclack::log::warning(message);
    } else {
        println!("  warning: {message}");
    }
}

pub(crate) fn error(message: impl Display) {
    if fancy() {
        let _ = cliclack::log::error(message);
    } else {
        eprintln!("{message}");
    }
}

pub(crate) fn confirm(prompt: &str, assume_yes: bool) -> bool {
    if assume_yes {
        info(format!("{prompt} yes (--yes)"));
        return true;
    }

    if can_prompt() {
        return cliclack::confirm(prompt)
            .initial_value(false)
            .interact()
            .unwrap_or(false);
    }

    print!("{prompt} [y/N] ");
    let _ = io::stdout().flush();
    let mut answer = String::new();
    if io::stdin().lock().read_line(&mut answer).is_err() {
        return false;
    }
    matches!(answer.trim().to_lowercase().as_str(), "y" | "yes")
}

pub(crate) fn activity<T, F>(start: impl Display, done: impl Display, f: F) -> io::Result<T>
where
    F: FnOnce() -> io::Result<T>,
{
    let start = start.to_string();
    let done = done.to_string();
    if fancy() {
        info(&start);
    }
    match f() {
        Ok(value) => {
            success(done);
            Ok(value)
        }
        Err(error) => {
            self::error(format!("{start}: failed"));
            Err(error)
        }
    }
}
