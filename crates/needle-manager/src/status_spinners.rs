use crate::config::{Config, StatuslineConfig, StatuslineStateConfig};
use crossterm::{
    cursor::{Hide, MoveTo, Show},
    event::{self, Event, KeyCode, KeyEvent, KeyModifiers},
    execute,
    terminal::{Clear, ClearType, disable_raw_mode, enable_raw_mode},
};
use std::collections::BTreeMap;
use std::io::{self, IsTerminal, Write};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub(crate) const STATUS_STATES: &[&str] = &["loading", "busy", "resident", "off", "failed"];
pub(crate) const MIN_INTERVAL_MS: u64 = 20;
pub(crate) const MAX_INTERVAL_MS: u64 = 2000;

const DEFAULTS: &[(&str, &str, &str)] = &[
    ("loading", "dots3", "amber"),
    ("busy", "dots2", "cyan"),
    ("resident", "simpleDots", "green"),
    ("off", "simpleDotsScrolling", "gray"),
    ("failed", "arc", "red"),
];

const FALLBACK_SPINNERS_JSON: &str = r#"{
  "dots3": {"interval": 80, "frames": ["⠋","⠙","⠚","⠞","⠖","⠦","⠴","⠲","⠳","⠓"]},
  "dots2": {"interval": 80, "frames": ["⣾","⣽","⣻","⢿","⡿","⣟","⣯","⣷"]},
  "simpleDots": {"interval": 400, "frames": [".  ",".. ","...","   "]},
  "simpleDotsScrolling": {"interval": 200, "frames": [".  ",".. ","..."," ..","  .","   "]},
  "arc": {"interval": 100, "frames": ["◜","◠","◝","◞","◡","◟"]}
}"#;

#[derive(Clone, Debug)]
pub(crate) struct SpinnerEntry {
    pub(crate) name: String,
    pub(crate) interval: u64,
    pub(crate) frames: Vec<String>,
}

#[derive(Clone, Debug)]
pub(crate) struct SpinnerCatalog {
    entries: Vec<SpinnerEntry>,
    source: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct StatuslineStateAppearance {
    pub(crate) spinner: String,
    pub(crate) color: String,
    pub(crate) interval_ms: Option<u64>,
}

pub(crate) type StatuslineDraft = BTreeMap<String, StatuslineStateAppearance>;

#[derive(Clone, Copy, Debug)]
pub(crate) struct PaletteColor {
    pub(crate) name: &'static str,
    ansi: &'static str,
}

pub(crate) const PALETTE: &[PaletteColor] = &[
    PaletteColor {
        name: "gray",
        ansi: "38;5;240",
    },
    PaletteColor {
        name: "amber",
        ansi: "38;5;179",
    },
    PaletteColor {
        name: "cyan",
        ansi: "38;5;87",
    },
    PaletteColor {
        name: "green",
        ansi: "38;5;35",
    },
    PaletteColor {
        name: "red",
        ansi: "38;5;196",
    },
    PaletteColor {
        name: "blue",
        ansi: "38;5;75",
    },
    PaletteColor {
        name: "white",
        ansi: "38;5;255",
    },
];

impl SpinnerCatalog {
    pub(crate) fn load() -> io::Result<Self> {
        let mut last_error = None;
        for path in spinner_json_candidates() {
            match std::fs::read_to_string(&path) {
                Ok(text) => return Self::from_json(&text, path.display().to_string()),
                Err(error) => last_error = Some((path, error)),
            }
        }
        match Self::from_json(FALLBACK_SPINNERS_JSON, "built-in fallback".to_string()) {
            Ok(catalog) => Ok(catalog),
            Err(error) => match last_error {
                Some((path, read_error)) => Err(io::Error::other(format!(
                    "failed to read cli-spinners catalog at {}: {read_error}; fallback failed: {error}",
                    path.display()
                ))),
                None => Err(io::Error::other(error)),
            },
        }
    }

    fn from_json(text: &str, source: String) -> io::Result<Self> {
        let data = serde_json::from_str::<serde_json::Value>(text).map_err(io::Error::other)?;
        let object = data
            .as_object()
            .ok_or_else(|| io::Error::other("cli-spinners catalog is not an object"))?;
        let mut entries = Vec::new();
        for (name, value) in object {
            let Some(interval) = value.get("interval").and_then(serde_json::Value::as_u64) else {
                continue;
            };
            let Some(frames) = value.get("frames").and_then(serde_json::Value::as_array) else {
                continue;
            };
            let frames = frames
                .iter()
                .filter_map(serde_json::Value::as_str)
                .map(ToOwned::to_owned)
                .collect::<Vec<_>>();
            if interval > 0 && !frames.is_empty() {
                entries.push(SpinnerEntry {
                    name: name.to_string(),
                    interval,
                    frames,
                });
            }
        }
        entries.sort_by(|left, right| left.name.cmp(&right.name));
        if entries.is_empty() {
            return Err(io::Error::other(
                "cli-spinners catalog has no usable entries",
            ));
        }
        Ok(Self { entries, source })
    }

    pub(crate) fn entries(&self) -> &[SpinnerEntry] {
        &self.entries
    }

    pub(crate) fn source(&self) -> &str {
        &self.source
    }

    pub(crate) fn get(&self, name: &str) -> Option<&SpinnerEntry> {
        self.entries.iter().find(|entry| entry.name == name)
    }

    pub(crate) fn contains(&self, name: &str) -> bool {
        self.get(name).is_some()
    }
}

pub(crate) fn default_statusline() -> StatuslineDraft {
    DEFAULTS
        .iter()
        .map(|(state, spinner, color)| {
            (
                (*state).to_string(),
                StatuslineStateAppearance {
                    spinner: (*spinner).to_string(),
                    color: (*color).to_string(),
                    interval_ms: None,
                },
            )
        })
        .collect()
}

pub(crate) fn has_structured_statusline(config: &Config) -> bool {
    !config.statusline.states.is_empty()
}

pub(crate) fn normalized_statusline(config: &Config, catalog: &SpinnerCatalog) -> StatuslineDraft {
    let mut draft = default_statusline();
    if let Some(legacy) = config
        .status_spinner
        .as_deref()
        .filter(|name| catalog.contains(name))
    {
        set_spinner(&mut draft, "loading", legacy, catalog);
        set_spinner(&mut draft, "busy", legacy, catalog);
    }
    for state in STATUS_STATES {
        if let Some(name) = config
            .status_spinners
            .get(*state)
            .filter(|name| catalog.contains(name))
        {
            set_spinner(&mut draft, state, name, catalog);
        }
    }
    for state in STATUS_STATES {
        if let Some(saved) = config.statusline.states.get(*state) {
            if let Some(spinner) = saved
                .spinner
                .as_deref()
                .filter(|name| catalog.contains(name))
            {
                set_spinner(&mut draft, state, spinner, catalog);
            }
            if let Some(color) = saved
                .color
                .as_deref()
                .filter(|name| palette_color(name).is_some())
            {
                if let Some(value) = draft.get_mut(*state) {
                    value.color = color.to_string();
                }
            }
            if let Some(interval) = saved.interval_ms.and_then(valid_interval_ms) {
                if let Some(value) = draft.get_mut(*state) {
                    value.interval_ms = Some(interval);
                    normalize_interval_for_state(value, catalog);
                }
            }
        }
    }
    draft
}

pub(crate) fn write_statusline_to_config(config: &mut Config, draft: &StatuslineDraft) {
    let mut states = BTreeMap::new();
    for state in STATUS_STATES {
        if let Some(appearance) = draft.get(*state) {
            states.insert(
                (*state).to_string(),
                StatuslineStateConfig {
                    spinner: Some(appearance.spinner.clone()),
                    color: Some(appearance.color.clone()),
                    interval_ms: appearance.interval_ms,
                },
            );
        }
    }
    config.statusline = StatuslineConfig { states };
    config.status_spinner = None;
    config.status_spinners.clear();
}

pub(crate) fn is_status_state(state: &str) -> bool {
    STATUS_STATES.contains(&state)
}

pub(crate) fn palette_color(name: &str) -> Option<PaletteColor> {
    PALETTE.iter().copied().find(|color| color.name == name)
}

pub(crate) fn valid_interval_ms(value: u64) -> Option<u64> {
    (MIN_INTERVAL_MS..=MAX_INTERVAL_MS)
        .contains(&value)
        .then_some(value)
}

pub(crate) fn set_spinner(
    draft: &mut StatuslineDraft,
    state: &str,
    spinner: &str,
    catalog: &SpinnerCatalog,
) {
    if !catalog.contains(spinner) {
        return;
    }
    if let Some(value) = draft.get_mut(state) {
        value.spinner = spinner.to_string();
        normalize_interval_for_state(value, catalog);
    }
}

pub(crate) fn reset_state(draft: &mut StatuslineDraft, state: &str) {
    let defaults = default_statusline();
    if let Some(default) = defaults.get(state).cloned() {
        draft.insert(state.to_string(), default);
    }
}

pub(crate) fn reset_all(draft: &mut StatuslineDraft) {
    *draft = default_statusline();
}

pub(crate) fn spinner_sequence(entry: &SpinnerEntry, max_frames: usize) -> String {
    entry
        .frames
        .iter()
        .take(max_frames)
        .map(String::as_str)
        .collect::<Vec<_>>()
        .join(" ")
}

pub(crate) fn preview_spinner(entry: &SpinnerEntry, label: &str, duration: Duration) {
    let mut stdout = io::stdout();
    if !stdout.is_terminal() {
        println!("{label}: {}", spinner_sequence(entry, 12));
        return;
    }

    let started = Instant::now();
    let mut index = 0usize;
    while started.elapsed() < duration {
        let frame = &entry.frames[index % entry.frames.len()];
        let _ = write!(stdout, "\r{label}: {frame} ");
        let _ = stdout.flush();
        index += 1;
        thread::sleep(Duration::from_millis(entry.interval));
    }
    let _ = writeln!(stdout, "\r{label}: {}", spinner_sequence(entry, 10));
}

pub(crate) fn print_spinner_list(catalog: &SpinnerCatalog) {
    println!("cli-spinners catalog: {}", catalog.source());
    for entry in catalog.entries() {
        println!(
            "{:<24} {:>4}ms {:>3} frames  {}",
            entry.name,
            entry.interval,
            entry.frames.len(),
            spinner_sequence(entry, 10)
        );
    }
}

pub(crate) fn statusline_summary(draft: &StatuslineDraft, catalog: &SpinnerCatalog) -> String {
    let mut body = String::new();
    for state in STATUS_STATES {
        let Some(appearance) = draft.get(*state) else {
            continue;
        };
        let Some(entry) = catalog.get(&appearance.spinner) else {
            continue;
        };
        let interval = interval_label(appearance, entry);
        body.push_str(&format!(
            "{:<9} {:<22} {:<6} {}\n",
            state, appearance.spinner, appearance.color, interval
        ));
    }
    body.trim_end().to_string()
}

pub(crate) fn edit_statusline(
    initial: &StatuslineDraft,
    catalog: &SpinnerCatalog,
) -> io::Result<Option<StatuslineDraft>> {
    let mut editor = Editor::new(initial.clone(), catalog);
    editor.run()
}

fn normalize_interval_for_state(
    appearance: &mut StatuslineStateAppearance,
    catalog: &SpinnerCatalog,
) {
    let Some(entry) = catalog.get(&appearance.spinner) else {
        appearance.interval_ms = None;
        return;
    };
    if appearance.interval_ms == Some(entry.interval) {
        appearance.interval_ms = None;
    }
}

fn effective_interval(appearance: &StatuslineStateAppearance, entry: &SpinnerEntry) -> u64 {
    appearance.interval_ms.unwrap_or(entry.interval)
}

fn interval_label(appearance: &StatuslineStateAppearance, entry: &SpinnerEntry) -> String {
    match appearance.interval_ms {
        Some(interval) => format!("{interval}ms"),
        None => format!("default {}ms", entry.interval),
    }
}

fn glyph_at(entry: &SpinnerEntry, interval_ms: u64, elapsed: Duration) -> &str {
    let tick = elapsed.as_millis() / u128::from(interval_ms.max(1));
    &entry.frames[(tick as usize) % entry.frames.len()]
}

fn colored_glyph(color: &str, glyph: &str) -> String {
    let ansi = palette_color(color).unwrap_or(PALETTE[0]).ansi;
    format!("\x1b[{ansi}m{glyph}\x1b[0m")
}

fn render_statusline_sample(
    draft: &StatuslineDraft,
    catalog: &SpinnerCatalog,
    elapsed: Duration,
) -> String {
    let state = sample_state_at(draft, catalog, elapsed);
    let appearance = draft
        .get(state)
        .or_else(|| draft.get("loading"))
        .expect("default draft has loading");
    let entry = catalog
        .get(&appearance.spinner)
        .or_else(|| catalog.get("dots3"))
        .unwrap_or_else(|| &catalog.entries()[0]);
    let glyph = glyph_at(entry, effective_interval(appearance, entry), elapsed);
    format!(
        "{} needle · 3.1k chars trimmed · 2 prunes",
        colored_glyph(&appearance.color, glyph)
    )
}

fn sample_state_at<'a>(
    draft: &'a StatuslineDraft,
    catalog: &SpinnerCatalog,
    elapsed: Duration,
) -> &'a str {
    let durations = STATUS_STATES
        .iter()
        .map(|state| {
            let Some(appearance) = draft.get(*state) else {
                return (*state, 1200_u128);
            };
            let Some(entry) = catalog.get(&appearance.spinner) else {
                return (*state, 1200_u128);
            };
            let cycle =
                u128::from(effective_interval(appearance, entry)) * entry.frames.len() as u128;
            (*state, (cycle * 3).clamp(700, 2400))
        })
        .collect::<Vec<_>>();
    let total: u128 = durations.iter().map(|(_, duration)| *duration).sum();
    let mut cursor = elapsed.as_millis() % total.max(1);
    for (state, duration) in durations {
        if cursor < duration {
            return state;
        }
        cursor -= duration;
    }
    "loading"
}

struct Editor<'a> {
    draft: StatuslineDraft,
    original: StatuslineDraft,
    catalog: &'a SpinnerCatalog,
    screen: Screen,
    started: Instant,
}

#[derive(Clone, Debug)]
enum Screen {
    Main {
        cursor: usize,
    },
    StateMenu {
        state: String,
        cursor: usize,
    },
    SpinnerPicker {
        state: String,
        query: String,
        cursor: usize,
        start: usize,
    },
    ColorPicker {
        state: String,
        cursor: usize,
    },
    IntervalPicker {
        state: String,
        cursor: usize,
        custom: String,
        entering_custom: bool,
    },
    ConfirmCancel,
}

impl<'a> Editor<'a> {
    fn new(draft: StatuslineDraft, catalog: &'a SpinnerCatalog) -> Self {
        Self {
            original: draft.clone(),
            draft,
            catalog,
            screen: Screen::Main { cursor: 0 },
            started: Instant::now(),
        }
    }

    fn run(&mut self) -> io::Result<Option<StatuslineDraft>> {
        let _guard = RawTerminalGuard::new()?;
        let mut stdout = io::stdout();
        loop {
            execute!(stdout, Clear(ClearType::All), MoveTo(0, 0), Hide)?;
            write!(stdout, "{}", self.render().replace('\n', "\r\n"))?;
            stdout.flush()?;
            if event::poll(Duration::from_millis(80))? {
                if let Event::Key(key) = event::read()? {
                    if let Some(outcome) = self.handle_key(key) {
                        execute!(stdout, Clear(ClearType::All), MoveTo(0, 0), Show)?;
                        return outcome;
                    }
                }
            }
        }
    }

    fn render(&self) -> String {
        let elapsed = self.started.elapsed();
        let mut out = String::new();
        out.push_str("◇ needle spinner\n");
        out.push_str("│\n");
        out.push_str("│ ");
        out.push_str(&render_statusline_sample(
            &self.draft,
            self.catalog,
            elapsed,
        ));
        out.push_str("\n│\n");
        match &self.screen {
            Screen::Main { cursor } => self.render_main(&mut out, *cursor, elapsed),
            Screen::StateMenu { state, cursor } => {
                self.render_state_menu(&mut out, state, *cursor, elapsed)
            }
            Screen::SpinnerPicker {
                state,
                query,
                cursor,
                start,
            } => self.render_spinner_picker(&mut out, state, query, *cursor, *start, elapsed),
            Screen::ColorPicker { state, cursor } => {
                self.render_color_picker(&mut out, state, *cursor, elapsed)
            }
            Screen::IntervalPicker {
                state,
                cursor,
                custom,
                entering_custom,
            } => self.render_interval_picker(&mut out, state, *cursor, custom, *entering_custom),
            Screen::ConfirmCancel => {
                out.push_str("◆ Discard unsaved changes?\n");
                out.push_str("│ y discard   n keep editing\n");
            }
        }
        out.push_str("\n");
        out.push_str("  ↑/↓ move · enter choose · esc/backspace back · s save · q cancel\n");
        out
    }

    fn render_main(&self, out: &mut String, cursor: usize, elapsed: Duration) {
        out.push_str("◆ Statusline states\n");
        for (index, state) in STATUS_STATES.iter().enumerate() {
            let Some(appearance) = self.draft.get(*state) else {
                continue;
            };
            let Some(entry) = self.catalog.get(&appearance.spinner) else {
                continue;
            };
            let glyph = glyph_at(entry, effective_interval(appearance, entry), elapsed);
            row(
                out,
                cursor == index,
                &format!(
                    "{:<9} {}  {:<22} {:<6} {}",
                    state,
                    colored_glyph(&appearance.color, glyph),
                    appearance.spinner,
                    appearance.color,
                    interval_label(appearance, entry)
                ),
            );
        }
        let base = STATUS_STATES.len();
        row(out, cursor == base, "reset all states");
        row(out, cursor == base + 1, "save and exit");
        row(out, cursor == base + 2, "cancel");
    }

    fn render_state_menu(&self, out: &mut String, state: &str, cursor: usize, elapsed: Duration) {
        let appearance = self.draft.get(state).expect("valid state");
        let entry = self
            .catalog
            .get(&appearance.spinner)
            .expect("valid spinner");
        let glyph = glyph_at(entry, effective_interval(appearance, entry), elapsed);
        out.push_str(&format!("◆ Edit {state}\n"));
        row(
            out,
            cursor == 0,
            &format!(
                "spinner   {}  {}",
                colored_glyph(&appearance.color, glyph),
                appearance.spinner
            ),
        );
        row(out, cursor == 1, &format!("colour    {}", appearance.color));
        row(
            out,
            cursor == 2,
            &format!("interval  {}", interval_label(appearance, entry)),
        );
        row(out, cursor == 3, "reset this state");
        row(out, cursor == 4, "back");
    }

    fn render_spinner_picker(
        &self,
        out: &mut String,
        state: &str,
        query: &str,
        cursor: usize,
        start: usize,
        elapsed: Duration,
    ) {
        out.push_str(&format!("◆ Spinner for {state}\n"));
        out.push_str(&format!("│ filter: {query}\n"));
        let matches = self.spinner_matches(query);
        if matches.is_empty() {
            out.push_str("│ no matches\n");
            return;
        }
        for (visible, index) in matches.iter().skip(start).take(10).enumerate() {
            let selected = start + visible == cursor;
            let entry = &self.catalog.entries()[*index];
            let glyph = glyph_at(entry, entry.interval, elapsed);
            row(
                out,
                selected,
                &format!(
                    "{}  {:<24} {:>4}ms {:>3} frames",
                    glyph,
                    entry.name,
                    entry.interval,
                    entry.frames.len()
                ),
            );
        }
    }

    fn render_color_picker(&self, out: &mut String, state: &str, cursor: usize, elapsed: Duration) {
        out.push_str(&format!("◆ Colour for {state}\n"));
        let appearance = self.draft.get(state).expect("valid state");
        let entry = self
            .catalog
            .get(&appearance.spinner)
            .expect("valid spinner");
        let glyph = glyph_at(entry, effective_interval(appearance, entry), elapsed);
        for (index, color) in PALETTE.iter().enumerate() {
            row(
                out,
                cursor == index,
                &format!("{}  {}", colored_glyph(color.name, glyph), color.name),
            );
        }
    }

    fn render_interval_picker(
        &self,
        out: &mut String,
        state: &str,
        cursor: usize,
        custom: &str,
        entering_custom: bool,
    ) {
        let appearance = self.draft.get(state).expect("valid state");
        let entry = self
            .catalog
            .get(&appearance.spinner)
            .expect("valid spinner");
        let fast = faster_interval(entry.interval);
        let slow = slower_interval(entry.interval);
        out.push_str(&format!("◆ Interval for {state}\n"));
        row(
            out,
            cursor == 0,
            &format!("package default  {}ms", entry.interval),
        );
        row(out, cursor == 1, &format!("faster           {fast}ms"));
        row(out, cursor == 2, &format!("slower           {slow}ms"));
        let marker = if entering_custom {
            "custom ms: "
        } else {
            "custom"
        };
        row(out, cursor == 3, &format!("{marker}{custom}"));
        out.push_str(&format!(
            "│ current: {}\n",
            interval_label(appearance, entry)
        ));
        out.push_str(&format!(
            "│ bounds: {MIN_INTERVAL_MS}..{MAX_INTERVAL_MS}ms\n"
        ));
    }

    fn handle_key(&mut self, key: KeyEvent) -> Option<io::Result<Option<StatuslineDraft>>> {
        if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
            return Some(Ok(None));
        }
        let screen = self.screen.clone();
        match screen {
            Screen::Main { mut cursor } => match key.code {
                KeyCode::Char('s') => Some(Ok(Some(self.draft.clone()))),
                KeyCode::Char('q') | KeyCode::Esc => {
                    if self.draft == self.original {
                        Some(Ok(None))
                    } else {
                        self.screen = Screen::ConfirmCancel;
                        None
                    }
                }
                KeyCode::Up | KeyCode::Char('k') => {
                    cursor = cursor.saturating_sub(1);
                    self.screen = Screen::Main { cursor };
                    None
                }
                KeyCode::Down | KeyCode::Char('j') => {
                    cursor = (cursor + 1).min(STATUS_STATES.len() + 2);
                    self.screen = Screen::Main { cursor };
                    None
                }
                KeyCode::Enter => {
                    if cursor < STATUS_STATES.len() {
                        self.screen = Screen::StateMenu {
                            state: STATUS_STATES[cursor].to_string(),
                            cursor: 0,
                        };
                    } else if cursor == STATUS_STATES.len() {
                        reset_all(&mut self.draft);
                    } else if cursor == STATUS_STATES.len() + 1 {
                        return Some(Ok(Some(self.draft.clone())));
                    } else if self.draft == self.original {
                        return Some(Ok(None));
                    } else {
                        self.screen = Screen::ConfirmCancel;
                    }
                    None
                }
                KeyCode::Char('r') if cursor < STATUS_STATES.len() => {
                    reset_state(&mut self.draft, STATUS_STATES[cursor]);
                    None
                }
                _ => None,
            },
            Screen::StateMenu { state, mut cursor } => match key.code {
                KeyCode::Esc | KeyCode::Backspace => {
                    self.screen = Screen::Main { cursor: 0 };
                    None
                }
                KeyCode::Up | KeyCode::Char('k') => {
                    cursor = cursor.saturating_sub(1);
                    self.screen = Screen::StateMenu { state, cursor };
                    None
                }
                KeyCode::Down | KeyCode::Char('j') => {
                    cursor = (cursor + 1).min(4);
                    self.screen = Screen::StateMenu { state, cursor };
                    None
                }
                KeyCode::Enter => {
                    match cursor {
                        0 => {
                            self.screen = Screen::SpinnerPicker {
                                state,
                                query: String::new(),
                                cursor: 0,
                                start: 0,
                            };
                        }
                        1 => {
                            let color = self
                                .draft
                                .get(&state)
                                .and_then(|value| {
                                    PALETTE.iter().position(|color| color.name == value.color)
                                })
                                .unwrap_or(0);
                            self.screen = Screen::ColorPicker {
                                state,
                                cursor: color,
                            };
                        }
                        2 => {
                            self.screen = Screen::IntervalPicker {
                                state,
                                cursor: 0,
                                custom: String::new(),
                                entering_custom: false,
                            };
                        }
                        3 => reset_state(&mut self.draft, &state),
                        _ => self.screen = Screen::Main { cursor: 0 },
                    }
                    None
                }
                _ => None,
            },
            Screen::SpinnerPicker {
                state,
                mut query,
                mut cursor,
                mut start,
            } => {
                let mut changed_query = false;
                match key.code {
                    KeyCode::Esc => {
                        self.screen = Screen::StateMenu { state, cursor: 0 };
                        return None;
                    }
                    KeyCode::Backspace => {
                        query.pop();
                        cursor = 0;
                        start = 0;
                        changed_query = true;
                    }
                    KeyCode::Up | KeyCode::Char('k') => {
                        cursor = cursor.saturating_sub(1);
                    }
                    KeyCode::Down | KeyCode::Char('j') => {
                        let len = self.spinner_matches(&query).len();
                        cursor = (cursor + 1).min(len.saturating_sub(1));
                    }
                    KeyCode::Enter => {
                        let matches = self.spinner_matches(&query);
                        if let Some(index) = matches.get(cursor) {
                            let name = self.catalog.entries()[*index].name.clone();
                            set_spinner(&mut self.draft, &state, &name, self.catalog);
                            self.screen = Screen::StateMenu { state, cursor: 0 };
                            return None;
                        }
                    }
                    KeyCode::Char(ch) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                        query.push(ch);
                        cursor = 0;
                        start = 0;
                        changed_query = true;
                    }
                    _ => {}
                }
                if !changed_query {
                    if cursor < start {
                        start = cursor;
                    } else if cursor >= start + 10 {
                        start = cursor.saturating_sub(9);
                    }
                }
                self.screen = Screen::SpinnerPicker {
                    state,
                    query,
                    cursor,
                    start,
                };
                None
            }
            Screen::ColorPicker { state, mut cursor } => match key.code {
                KeyCode::Esc | KeyCode::Backspace => {
                    self.screen = Screen::StateMenu { state, cursor: 1 };
                    None
                }
                KeyCode::Up | KeyCode::Char('k') => {
                    cursor = cursor.saturating_sub(1);
                    self.screen = Screen::ColorPicker { state, cursor };
                    None
                }
                KeyCode::Down | KeyCode::Char('j') => {
                    cursor = (cursor + 1).min(PALETTE.len().saturating_sub(1));
                    self.screen = Screen::ColorPicker { state, cursor };
                    None
                }
                KeyCode::Enter => {
                    if let Some(value) = self.draft.get_mut(&state) {
                        value.color = PALETTE[cursor].name.to_string();
                    }
                    self.screen = Screen::StateMenu { state, cursor: 1 };
                    None
                }
                _ => None,
            },
            Screen::IntervalPicker {
                state,
                mut cursor,
                mut custom,
                mut entering_custom,
            } => match key.code {
                KeyCode::Esc => {
                    if entering_custom {
                        entering_custom = false;
                        self.screen = Screen::IntervalPicker {
                            state,
                            cursor,
                            custom,
                            entering_custom,
                        };
                    } else {
                        self.screen = Screen::StateMenu { state, cursor: 2 };
                    }
                    None
                }
                KeyCode::Backspace if entering_custom => {
                    custom.pop();
                    self.screen = Screen::IntervalPicker {
                        state,
                        cursor,
                        custom,
                        entering_custom,
                    };
                    None
                }
                KeyCode::Backspace => {
                    self.screen = Screen::StateMenu { state, cursor: 2 };
                    None
                }
                KeyCode::Up | KeyCode::Char('k') if !entering_custom => {
                    cursor = cursor.saturating_sub(1);
                    self.screen = Screen::IntervalPicker {
                        state,
                        cursor,
                        custom,
                        entering_custom,
                    };
                    None
                }
                KeyCode::Down | KeyCode::Char('j') if !entering_custom => {
                    cursor = (cursor + 1).min(3);
                    self.screen = Screen::IntervalPicker {
                        state,
                        cursor,
                        custom,
                        entering_custom,
                    };
                    None
                }
                KeyCode::Char(ch) if entering_custom && ch.is_ascii_digit() => {
                    custom.push(ch);
                    self.screen = Screen::IntervalPicker {
                        state,
                        cursor,
                        custom,
                        entering_custom,
                    };
                    None
                }
                KeyCode::Enter => {
                    let entry = self
                        .draft
                        .get(&state)
                        .and_then(|appearance| self.catalog.get(&appearance.spinner))
                        .expect("valid spinner");
                    if cursor == 3 && !entering_custom {
                        entering_custom = true;
                        custom.clear();
                        self.screen = Screen::IntervalPicker {
                            state,
                            cursor,
                            custom,
                            entering_custom,
                        };
                        return None;
                    }
                    let selected = if cursor == 0 {
                        Some(None)
                    } else if cursor == 1 {
                        Some(Some(faster_interval(entry.interval)))
                    } else if cursor == 2 {
                        Some(Some(slower_interval(entry.interval)))
                    } else {
                        custom
                            .parse::<u64>()
                            .ok()
                            .and_then(valid_interval_ms)
                            .map(Some)
                    };
                    if let Some(interval_ms) = selected {
                        if let Some(value) = self.draft.get_mut(&state) {
                            value.interval_ms = interval_ms;
                            normalize_interval_for_state(value, self.catalog);
                        }
                        self.screen = Screen::StateMenu { state, cursor: 2 };
                    }
                    None
                }
                _ => None,
            },
            Screen::ConfirmCancel => match key.code {
                KeyCode::Char('y') | KeyCode::Char('Y') => Some(Ok(None)),
                KeyCode::Char('n') | KeyCode::Char('N') | KeyCode::Esc => {
                    self.screen = Screen::Main { cursor: 0 };
                    None
                }
                _ => None,
            },
        }
    }

    fn spinner_matches(&self, query: &str) -> Vec<usize> {
        let query = query.trim().to_lowercase();
        let mut scored = self
            .catalog
            .entries()
            .iter()
            .enumerate()
            .filter_map(|(index, entry)| {
                if query.is_empty() {
                    return Some((index, 0usize));
                }
                fuzzy_score(&entry.name.to_lowercase(), &query).map(|score| (index, score))
            })
            .collect::<Vec<_>>();
        scored.sort_by(|left, right| {
            left.1.cmp(&right.1).then_with(|| {
                self.catalog.entries()[left.0]
                    .name
                    .cmp(&self.catalog.entries()[right.0].name)
            })
        });
        scored.into_iter().map(|(index, _)| index).collect()
    }
}

struct RawTerminalGuard;

impl RawTerminalGuard {
    fn new() -> io::Result<Self> {
        enable_raw_mode()?;
        execute!(io::stdout(), Hide)?;
        Ok(Self)
    }
}

impl Drop for RawTerminalGuard {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(io::stdout(), Show);
    }
}

fn row(out: &mut String, selected: bool, text: &str) {
    if selected {
        out.push_str(&format!("│ › {text}\n"));
    } else {
        out.push_str(&format!("│   {text}\n"));
    }
}

fn fuzzy_score(name: &str, query: &str) -> Option<usize> {
    let mut score = 0usize;
    let mut last = 0usize;
    for ch in query.chars() {
        let found = name[last..].find(ch)?;
        score += found;
        last += found + ch.len_utf8();
    }
    Some(score + name.len().saturating_sub(query.len()))
}

fn faster_interval(default_ms: u64) -> u64 {
    valid_interval_ms((default_ms / 2).max(MIN_INTERVAL_MS)).unwrap_or(MIN_INTERVAL_MS)
}

fn slower_interval(default_ms: u64) -> u64 {
    valid_interval_ms((default_ms * 2).min(MAX_INTERVAL_MS)).unwrap_or(MAX_INTERVAL_MS)
}

fn spinner_json_candidates() -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    if let Some(path) = std::env::var_os("NEEDLE_SPINNERS_JSON").map(PathBuf::from) {
        candidates.push(path);
    }
    if let Some(path) = dev_path("pi/node_modules/cli-spinners/spinners.json") {
        candidates.push(path);
    }
    if let Some(exe) = std::env::current_exe().ok() {
        if let Some(prefix) = exe.parent().and_then(Path::parent) {
            candidates.push(prefix.join("share/needle/pi/node_modules/cli-spinners/spinners.json"));
        }
        for ancestor in exe.ancestors() {
            candidates.push(ancestor.join("pi/node_modules/cli-spinners/spinners.json"));
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        for ancestor in cwd.ancestors() {
            candidates.push(ancestor.join("pi/node_modules/cli-spinners/spinners.json"));
        }
    }
    dedupe_existing_order(candidates)
}

fn dev_path(relative: &str) -> Option<PathBuf> {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()?
        .parent()
        .map(|root| root.join(relative))
}

fn dedupe_existing_order(candidates: Vec<PathBuf>) -> Vec<PathBuf> {
    let mut seen = Vec::<PathBuf>::new();
    let mut unique = Vec::new();
    for candidate in candidates {
        if seen.iter().any(|existing| existing == &candidate) {
            continue;
        }
        seen.push(candidate.clone());
        unique.push(candidate);
    }
    unique
}

pub(crate) fn unique_temp_file(dir: &Path, name: &str) -> PathBuf {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    dir.join(format!("{name}-{}-{nanos}.json", std::process::id()))
}
