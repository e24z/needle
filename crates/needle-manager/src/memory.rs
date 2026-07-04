use std::env;
use std::path::{Path, PathBuf};

const DEFAULT_COLD_LOAD_MIN_AVAILABLE_MB: f64 = 2048.0;
const MODEL_LOAD_HEADROOM_MB: f64 = 768.0;
const COLD_LOAD_MIN_ENV: &str = "NEEDLE_COLD_LOAD_MIN_AVAILABLE_MB";
const COMPAT_MIN_ENV: &str = "NEEDLE_MIN_AVAILABLE_MB";

#[derive(Clone, Debug)]
pub(crate) struct MemoryRefusal {
    available_mb: f64,
    min_available_mb: f64,
    source: &'static str,
}

impl MemoryRefusal {
    pub(crate) fn message(&self) -> String {
        format!(
            "memory pressure critical ({} MB available; minimum {} MB for cold model load; source {})",
            self.available_mb.round(),
            self.min_available_mb.round(),
            self.source
        )
    }
}

pub(crate) fn cold_load_refusal() -> Option<MemoryRefusal> {
    let min_available_mb = cold_load_min_available_mb();
    if min_available_mb <= 0.0 {
        return None;
    }
    let available = available_memory_mb()?;
    if available.available_mb >= min_available_mb {
        return None;
    }
    Some(MemoryRefusal {
        available_mb: available.available_mb,
        min_available_mb,
        source: available.source,
    })
}

fn cold_load_min_available_mb() -> f64 {
    env::var(COLD_LOAD_MIN_ENV)
        .ok()
        .or_else(|| env::var(COMPAT_MIN_ENV).ok())
        .and_then(|value| value.parse::<f64>().ok())
        .unwrap_or_else(default_cold_load_min_available_mb)
}

fn default_cold_load_min_available_mb() -> f64 {
    model_weight_mb()
        .map(|weight_mb| {
            (weight_mb + MODEL_LOAD_HEADROOM_MB).max(DEFAULT_COLD_LOAD_MIN_AVAILABLE_MB)
        })
        .unwrap_or(DEFAULT_COLD_LOAD_MIN_AVAILABLE_MB)
}

fn model_weight_mb() -> Option<f64> {
    let model_dir = configured_model_dir()?;
    let bytes = safetensor_bytes(&model_dir)?;
    Some(bytes as f64 / 1024.0 / 1024.0)
}

fn configured_model_dir() -> Option<PathBuf> {
    env::var_os("NEEDLE_MODEL_DIR")
        .map(PathBuf::from)
        .or_else(|| crate::config::load()?.model_dir)
}

fn safetensor_bytes(dir: &Path) -> Option<u64> {
    let mut total = 0_u64;
    for entry in std::fs::read_dir(dir).ok()? {
        let path = entry.ok()?.path();
        if path.extension().and_then(|value| value.to_str()) != Some("safetensors") {
            continue;
        }
        total = total.saturating_add(std::fs::metadata(path).ok()?.len());
    }
    if total == 0 { None } else { Some(total) }
}

#[derive(Clone, Copy, Debug)]
struct AvailableMemory {
    available_mb: f64,
    source: &'static str,
}

fn available_memory_mb() -> Option<AvailableMemory> {
    #[cfg(target_os = "macos")]
    {
        if let Some(available_mb) = available_mb_from_vm_stat_command() {
            return Some(AvailableMemory {
                available_mb,
                source: "vm_stat",
            });
        }
    }
    #[cfg(target_os = "linux")]
    {
        if let Some(available_mb) = available_mb_from_meminfo_file() {
            return Some(AvailableMemory {
                available_mb,
                source: "/proc/meminfo",
            });
        }
    }
    None
}

#[cfg(target_os = "macos")]
fn available_mb_from_vm_stat_command() -> Option<f64> {
    let output = std::process::Command::new("vm_stat").output().ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8(output.stdout).ok()?;
    available_mb_from_vm_stat(&text)
}

#[cfg(target_os = "linux")]
fn available_mb_from_meminfo_file() -> Option<f64> {
    let text = std::fs::read_to_string("/proc/meminfo").ok()?;
    available_mb_from_meminfo(&text)
}

#[cfg(any(test, target_os = "macos"))]
fn available_mb_from_vm_stat(text: &str) -> Option<f64> {
    let page_size = page_size_from_vm_stat(text).unwrap_or(4096);
    let mut reclaimable_pages = 0_u64;
    for line in text.lines() {
        let Some((label, pages)) = vm_stat_page_line(line) else {
            continue;
        };
        if matches!(label.as_str(), "free" | "inactive" | "speculative") {
            reclaimable_pages = reclaimable_pages.saturating_add(pages);
        }
    }
    if reclaimable_pages == 0 {
        return None;
    }
    Some(reclaimable_pages as f64 * page_size as f64 / 1024.0 / 1024.0)
}

#[cfg(any(test, target_os = "macos"))]
fn page_size_from_vm_stat(text: &str) -> Option<u64> {
    let start = text.find("page size of ")? + "page size of ".len();
    let digits: String = text[start..]
        .chars()
        .take_while(|character| character.is_ascii_digit())
        .collect();
    digits.parse().ok()
}

#[cfg(any(test, target_os = "macos"))]
fn vm_stat_page_line(line: &str) -> Option<(String, u64)> {
    let rest = line.trim().strip_prefix("Pages ")?;
    let (label, count) = rest.split_once(':')?;
    Some((label.trim().to_lowercase(), page_count(count)?))
}

#[cfg(any(test, target_os = "macos"))]
fn page_count(text: &str) -> Option<u64> {
    let digits: String = text
        .chars()
        .filter(|character| character.is_ascii_digit())
        .collect();
    digits.parse().ok()
}

#[cfg(any(test, target_os = "linux"))]
fn available_mb_from_meminfo(text: &str) -> Option<f64> {
    for line in text.lines() {
        let Some(rest) = line.strip_prefix("MemAvailable:") else {
            continue;
        };
        let kb = rest.split_whitespace().next()?.parse::<u64>().ok()?;
        return Some(kb as f64 / 1024.0);
    }
    None
}

#[cfg(test)]
mod tests {
    use super::{available_mb_from_meminfo, available_mb_from_vm_stat};

    #[test]
    fn vm_stat_counts_reclaimable_pages_without_file_backed_cache() {
        let text = "\
Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free:                               100.
Pages active:                             999.
Pages inactive:                            20.
Pages speculative:                          5.
File-backed pages:                      50000.
";

        let available_mb = available_mb_from_vm_stat(text).expect("vm_stat parses");

        assert_eq!(available_mb, 125.0 * 4096.0 / 1024.0 / 1024.0);
    }

    #[test]
    fn meminfo_uses_memavailable() {
        let text = "\
MemTotal:       65536000 kB
MemFree:         1024000 kB
MemAvailable:    2097152 kB
";

        assert_eq!(available_mb_from_meminfo(text), Some(2048.0));
    }
}
