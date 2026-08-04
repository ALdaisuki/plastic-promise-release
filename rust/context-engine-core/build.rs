use sha2::{Digest, Sha256};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};

fn collect_rust_sources(directory: &Path, paths: &mut Vec<PathBuf>) {
    let mut entries = fs::read_dir(directory)
        .expect("read Rust source directory")
        .map(|entry| entry.expect("read Rust source entry").path())
        .collect::<Vec<_>>();
    entries.sort();
    for path in entries {
        if path.is_dir() {
            collect_rust_sources(&path, paths);
        } else if path.extension().and_then(|value| value.to_str()) == Some("rs") {
            paths.push(path);
        }
    }
}

fn main() {
    let crate_root = PathBuf::from(env::var("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR"));
    let mut paths = vec![
        crate_root.join("Cargo.toml"),
        crate_root.join("Cargo.lock"),
        crate_root.join("build.rs"),
    ];
    collect_rust_sources(&crate_root.join("src"), &mut paths);
    paths.sort_by_key(|path| {
        path.strip_prefix(&crate_root)
            .expect("Rust source below crate root")
            .to_string_lossy()
            .replace('\\', "/")
    });

    let mut digest = Sha256::new();
    for path in paths {
        println!("cargo:rerun-if-changed={}", path.display());
        let label = path
            .strip_prefix(&crate_root)
            .expect("Rust source below crate root")
            .to_string_lossy()
            .replace('\\', "/")
            .into_bytes();
        let content = fs::read(&path).expect("read Rust build input");
        digest.update((label.len() as u64).to_be_bytes());
        digest.update(&label);
        digest.update((content.len() as u64).to_be_bytes());
        digest.update(&content);
    }
    println!("cargo:rustc-env=PP_RUST_SOURCE_SHA256={:x}", digest.finalize());
}
