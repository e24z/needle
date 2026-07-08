//! Update command scenarios against a local fake release archive.

use std::os::unix::fs::PermissionsExt;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};
use std::process::Command;

fn scratch(label: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("needle-update-{}-{label}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("create scratch");
    dir
}

fn chmod_executable(path: &Path) {
    let mut permissions = std::fs::metadata(path).unwrap().permissions();
    permissions.set_mode(0o755);
    std::fs::set_permissions(path, permissions).unwrap();
}

fn fake_release_archive(dir: &Path, version: &str) -> PathBuf {
    let root_name = format!("needle-{version}-aarch64-apple-darwin");
    let root = dir.join(&root_name);
    let bin = root.join("bin");
    let share = root.join("share").join("needle");
    std::fs::create_dir_all(&bin).expect("create fake bin");
    std::fs::create_dir_all(share.join("pi")).expect("create fake pi package");
    std::fs::create_dir_all(share.join("wheels")).expect("create fake wheels");

    let needle = bin.join("needle");
    std::fs::write(
        &needle,
        format!(
            r#"#!/bin/sh
if [ "$1" = "setup" ]; then
  echo "$@" >> "$NEEDLE_FAKE_SETUP_LOG"
  exit 0
fi
echo "needle {version}"
"#
        ),
    )
    .expect("write fake needle");
    chmod_executable(&needle);

    std::fs::write(
        share.join("pi").join("package.json"),
        r#"{"name":"needle","pi":{"extensions":["./extension.js"]}}"#,
    )
    .expect("write package");
    std::fs::write(share.join("pi").join("extension.js"), "").expect("write extension");
    std::fs::write(
        share
            .join("wheels")
            .join(format!("needle_worker-{version}-py3-none-any.whl")),
        "",
    )
    .expect("write wheel marker");

    let archive = dir.join(format!("needle-{version}.tar.gz"));
    let output = Command::new("tar")
        .arg("-czf")
        .arg(&archive)
        .arg("-C")
        .arg(dir)
        .arg(root_name)
        .output()
        .expect("create tarball");
    assert!(
        output.status.success(),
        "tar failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    archive
}

fn old_install(prefix: &Path) {
    std::fs::create_dir_all(prefix.join("bin")).expect("create bin");
    std::fs::create_dir_all(prefix.join("share").join("needle")).expect("create share");
    let old = prefix.join("bin").join("needle");
    std::fs::write(&old, "#!/bin/sh\necho old\n").expect("write old needle");
    chmod_executable(&old);
    std::fs::write(
        prefix.join("share").join("needle").join("stale.txt"),
        "stale",
    )
    .expect("write stale share file");
}

fn malicious_archive_with_parent_path(dir: &Path) -> PathBuf {
    let archive = dir.join("needle-bad-parent.tar.gz");
    let script = r#"
import io
import sys
import tarfile

archive = sys.argv[1]
data = b"bad"
with tarfile.open(archive, "w:gz") as tf:
    info = tarfile.TarInfo("../needle-owned-outside-prefix")
    info.size = len(data)
    tf.addfile(info, io.BytesIO(data))
"#;
    let output = Command::new("python3")
        .arg("-c")
        .arg(script)
        .arg(&archive)
        .output()
        .expect("create malicious tarball");
    assert!(
        output.status.success(),
        "python failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    archive
}

fn malicious_archive_with_symlink(dir: &Path) -> PathBuf {
    let archive = dir.join("needle-bad-symlink.tar.gz");
    let script = r#"
import sys
import tarfile

archive = sys.argv[1]
with tarfile.open(archive, "w:gz") as tf:
    root = tarfile.TarInfo("needle-bad/")
    root.type = tarfile.DIRTYPE
    tf.addfile(root)
    link = tarfile.TarInfo("needle-bad/link")
    link.type = tarfile.SYMTYPE
    link.linkname = "../outside"
    tf.addfile(link)
"#;
    let output = Command::new("python3")
        .arg("-c")
        .arg(script)
        .arg(&archive)
        .output()
        .expect("create malicious tarball");
    assert!(
        output.status.success(),
        "python failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    archive
}

#[test]
fn update_installs_archive_into_prefix() {
    let dir = scratch("install");
    let prefix = dir.join("prefix");
    old_install(&prefix);
    let archive = fake_release_archive(&dir, "9.9.9");

    let output = Command::new(env!("CARGO_BIN_EXE_needle"))
        .arg("update")
        .arg("--archive-url")
        .arg(&archive)
        .arg("--prefix")
        .arg(&prefix)
        .arg("--no-setup")
        .arg("--yes")
        .output()
        .expect("run update");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        output.status.success(),
        "stdout: {stdout}\nstderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        stdout.contains("needle update complete"),
        "stdout: {stdout}"
    );
    assert!(
        !prefix
            .join("share")
            .join("needle")
            .join("stale.txt")
            .exists(),
        "old share payload survived"
    );
    assert!(
        prefix
            .join("share")
            .join("needle")
            .join("pi")
            .join("package.json")
            .exists(),
        "new share payload missing"
    );

    let version = Command::new(prefix.join("bin").join("needle"))
        .output()
        .expect("run installed fake needle");
    assert_eq!(String::from_utf8_lossy(&version.stdout), "needle 9.9.9\n");
}

#[test]
fn update_dry_run_touches_nothing() {
    let dir = scratch("dry");
    let prefix = dir.join("prefix");
    old_install(&prefix);
    let archive = fake_release_archive(&dir, "9.9.9");

    let output = Command::new(env!("CARGO_BIN_EXE_needle"))
        .arg("update")
        .arg("--archive-url")
        .arg(&archive)
        .arg("--prefix")
        .arg(&prefix)
        .arg("--dry-run")
        .output()
        .expect("run update dry-run");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(output.status.success(), "stdout: {stdout}");
    assert!(stdout.contains("dry run complete"), "stdout: {stdout}");
    assert_eq!(
        std::fs::read_to_string(prefix.join("bin").join("needle")).unwrap(),
        "#!/bin/sh\necho old\n"
    );
    assert!(
        prefix
            .join("share")
            .join("needle")
            .join("stale.txt")
            .exists(),
        "dry run changed share payload"
    );
}

#[test]
fn update_runs_setup_after_install() {
    let dir = scratch("setup");
    let prefix = dir.join("prefix");
    old_install(&prefix);
    let archive = fake_release_archive(&dir, "9.9.9");
    let setup_log = dir.join("setup.log");

    let output = Command::new(env!("CARGO_BIN_EXE_needle"))
        .arg("update")
        .arg("--archive-url")
        .arg(&archive)
        .arg("--prefix")
        .arg(&prefix)
        .arg("--yes")
        .env("NEEDLE_FAKE_SETUP_LOG", &setup_log)
        .output()
        .expect("run update");

    let stdout = String::from_utf8_lossy(&output.stdout);
    assert!(
        output.status.success(),
        "stdout: {stdout}\nstderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        std::fs::read_to_string(setup_log).expect("setup log"),
        "setup --refresh-install --yes\n"
    );
}

#[test]
fn update_rejects_archive_parent_paths_before_extracting() {
    let dir = scratch("bad-parent");
    let prefix = dir.join("prefix");
    old_install(&prefix);
    let archive = malicious_archive_with_parent_path(&dir);

    let output = Command::new(env!("CARGO_BIN_EXE_needle"))
        .arg("update")
        .arg("--archive-url")
        .arg(&archive)
        .arg("--prefix")
        .arg(&prefix)
        .arg("--no-setup")
        .arg("--yes")
        .output()
        .expect("run update");

    assert!(
        !output.status.success(),
        "unsafe archive unexpectedly succeeded"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("invalid path") || stderr.contains("unsafe path"),
        "stderr: {stderr}"
    );
    assert!(
        !dir.join("needle-owned-outside-prefix").exists(),
        "unsafe archive wrote outside extraction root"
    );
    assert!(
        prefix
            .join("share")
            .join("needle")
            .join("stale.txt")
            .exists(),
        "failed update changed existing share payload"
    );
}

#[test]
fn update_rejects_archive_symlinks() {
    let dir = scratch("bad-symlink");
    let prefix = dir.join("prefix");
    old_install(&prefix);
    let archive = malicious_archive_with_symlink(&dir);

    let output = Command::new(env!("CARGO_BIN_EXE_needle"))
        .arg("update")
        .arg("--archive-url")
        .arg(&archive)
        .arg("--prefix")
        .arg(&prefix)
        .arg("--no-setup")
        .arg("--yes")
        .output()
        .expect("run update");

    assert!(
        !output.status.success(),
        "symlink archive unexpectedly succeeded"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("unsupported entry type"),
        "stderr: {stderr}"
    );
}

#[test]
fn update_rejects_symlinked_share_without_mutating_payload() {
    let dir = scratch("share-symlink");
    let prefix = dir.join("prefix");
    old_install(&prefix);
    let archive = fake_release_archive(&dir, "9.9.9");
    let external_share = dir.join("external-share");
    std::fs::create_dir_all(&external_share).expect("create external share");
    std::fs::write(external_share.join("owned.txt"), "owned").expect("write marker");
    std::fs::remove_dir_all(prefix.join("share").join("needle")).expect("remove share dir");
    symlink(&external_share, prefix.join("share").join("needle")).expect("symlink share");

    let output = Command::new(env!("CARGO_BIN_EXE_needle"))
        .arg("update")
        .arg("--archive-url")
        .arg(&archive)
        .arg("--prefix")
        .arg(&prefix)
        .arg("--no-setup")
        .arg("--yes")
        .output()
        .expect("run update");

    assert!(
        !output.status.success(),
        "symlinked share unexpectedly updated"
    );
    assert!(
        std::fs::symlink_metadata(prefix.join("share").join("needle"))
            .unwrap()
            .file_type()
            .is_symlink(),
        "share symlink was replaced"
    );
    assert!(
        external_share.join("owned.txt").exists(),
        "external share target was modified"
    );
}

#[test]
fn update_rejects_symlinked_binary_before_mutating_share() {
    let dir = scratch("bin-symlink");
    let prefix = dir.join("prefix");
    old_install(&prefix);
    let archive = fake_release_archive(&dir, "9.9.9");
    let external_bin = dir.join("external-needle");
    std::fs::write(&external_bin, "#!/bin/sh\necho external\n").expect("write external bin");
    chmod_executable(&external_bin);
    std::fs::remove_file(prefix.join("bin").join("needle")).expect("remove bin");
    symlink(&external_bin, prefix.join("bin").join("needle")).expect("symlink bin");

    let output = Command::new(env!("CARGO_BIN_EXE_needle"))
        .arg("update")
        .arg("--archive-url")
        .arg(&archive)
        .arg("--prefix")
        .arg(&prefix)
        .arg("--no-setup")
        .arg("--yes")
        .output()
        .expect("run update");

    assert!(
        !output.status.success(),
        "symlinked binary unexpectedly updated"
    );
    assert!(
        prefix
            .join("share")
            .join("needle")
            .join("stale.txt")
            .exists(),
        "share changed before binary symlink failure"
    );
}
