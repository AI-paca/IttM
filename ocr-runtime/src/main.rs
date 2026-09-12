use anyhow::{Context, Result, ensure};
use clap::{Parser, Subcommand};
use ittm_ocr_runtime::{
    document::{self, Limits},
    pipeline::{self, Checkpoint, Engines, Options},
};
use ittm_pipeline_core::{ABI_VERSION, SEPARATED_ROUTE_ID, session::Session};
use std::{
    io::{self, Write},
    path::PathBuf,
    sync::{Arc, atomic::AtomicBool},
};

#[derive(Parser)]
#[command(
    name = "ittm-ocr",
    about = "Native document OCR using the shared Rust pipeline"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}
#[derive(Subcommand)]
enum Command {
    Serve {
        #[arg(long, env = "OCR_HOST", default_value = "127.0.0.1")]
        host: String,
        #[arg(long, env = "PORT", default_value_t = 8000)]
        port: u16,
        #[arg(long, env = "OCR_CONCURRENCY", default_value_t = 1)]
        concurrency: usize,
        #[arg(long)]
        port_file: Option<PathBuf>,
    },
    Convert {
        source: PathBuf,
        #[arg(long, default_value = "auto")]
        engine: String,
        #[arg(long, default_value = "auto")]
        pdf_mode: String,
        #[arg(long)]
        profile: Option<String>,
        #[arg(long)]
        flags: Option<String>,
        #[arg(long)]
        stream: bool,
        #[arg(long)]
        output: Option<PathBuf>,
    },
    Debug {
        source: PathBuf,
        #[arg(long)]
        output: PathBuf,
        #[arg(long, default_value = "tesseract")]
        engine: String,
        #[arg(long)]
        profile: Option<String>,
        #[arg(long)]
        plan_only: bool,
        #[arg(long)]
        resume: Option<PathBuf>,
    },
    Check,
}
#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    let limits = Limits::from_env()?;
    match cli.command {
        Command::Serve {
            host,
            port,
            concurrency,
            port_file,
        } => {
            let app = ittm_ocr_runtime::server::router(limits, concurrency)?;
            let listener = tokio::net::TcpListener::bind((host.as_str(), port)).await?;
            let address = listener.local_addr()?;
            if let Some(path) = port_file {
                std::fs::write(path, address.port().to_string())?;
            }
            eprintln!("Rust OCR listening on http://{address}");
            axum::serve(listener, app)
                .with_graceful_shutdown(shutdown())
                .await?;
        }
        Command::Convert {
            source,
            engine,
            pdf_mode,
            profile,
            flags,
            stream,
            output,
        } => {
            let options = Options {
                engine,
                pdf_mode,
                pipeline_profile: profile,
                pipeline_flags: flags,
            };
            let cancel = Arc::new(AtomicBool::new(false));
            let result = pipeline::convert_file(
                &source,
                &source.file_name().unwrap_or_default().to_string_lossy(),
                &options,
                &limits,
                cancel,
                |event| {
                    if stream {
                        let mut stdout = io::stdout().lock();
                        serde_json::to_writer(&mut stdout, &event)?;
                        writeln!(stdout)?;
                        stdout.flush()?;
                    }
                    Ok(())
                },
            )?;
            if let Some(output) = output {
                std::fs::write(output, &result.markdown)?;
            } else if !stream {
                println!("{}", result.markdown);
            }
        }
        Command::Debug {
            source,
            output,
            engine,
            profile,
            plan_only,
            resume,
        } => {
            document::validate_file(&source, &limits)?;
            let image = document::decode(&source, &limits)?;
            let options = Options {
                engine: engine.clone(),
                pipeline_profile: profile,
                ..Options::default()
            };
            let mut checkpoint = Checkpoint {
                abi: ABI_VERSION,
                route: SEPARATED_ROUTE_ID,
                width: image.width,
                height: image.height,
                profile: options.profile()?,
                engine,
                attempts: Vec::new(),
            };
            let replay = if let Some(path) = resume {
                let prior: Checkpoint = serde_json::from_reader(std::fs::File::open(&path)?)?;
                ensure!(
                    prior.abi == ABI_VERSION
                        && prior.route == SEPARATED_ROUTE_ID
                        && prior.width == image.width
                        && prior.height == image.height,
                    "Checkpoint ABI/route/image dimensions differ"
                );
                // Bind replay to exact source pixels, not merely to similar dimensions.
                let previous_image = document::decode(
                    &path
                        .parent()
                        .context("Checkpoint has no parent")?
                        .join("source.png"),
                    &limits,
                )?;
                ensure!(
                    previous_image.pixels == image.pixels,
                    "Checkpoint source pixels differ"
                );
                checkpoint.profile = prior.profile;
                checkpoint.engine = prior.engine;
                prior.attempts
            } else {
                Vec::new()
            };
            std::fs::create_dir_all(&output)?;
            image::save_buffer(
                output.join("source.png"),
                &image.pixels,
                image.width,
                image.height,
                image::ColorType::Rgb8,
            )?;
            write_checkpoint(&output, &checkpoint)?;
            if plan_only {
                let session = Session::plan(&image).map_err(anyhow::Error::msg)?;
                std::fs::write(
                    output.join("stages.json"),
                    serde_json::to_vec_pretty(
                        &serde_json::json!({"stage_mask":session.stage_mask(),"next_stage":"ocr-blocks"}),
                    )?,
                )?;
            } else {
                let cancel = Arc::new(AtomicBool::new(false));
                let mut engines = Engines::new(&limits, cancel.clone());
                let profile = checkpoint.profile.clone();
                let engine = checkpoint.engine.clone();
                let result = pipeline::raster(
                    &image,
                    &profile,
                    &engine,
                    &mut engines,
                    &cancel,
                    &replay,
                    |attempt, raster| {
                        image::save_buffer(
                            output.join(format!("job-{:05}.png", attempt.index)),
                            &raster.pixels,
                            raster.width,
                            raster.height,
                            image::ColorType::Rgb8,
                        )?;
                        checkpoint.attempts.push(attempt.clone());
                        write_checkpoint(&output, &checkpoint)
                    },
                )?;
                std::fs::write(output.join("result.md"), result.markdown)?;
                std::fs::write(
                    output.join("stages.json"),
                    serde_json::to_vec_pretty(
                        &serde_json::json!({"stage_mask":255,"stages":ittm_pipeline_core::session::STAGES,"chunks":result.chunks,"tables_found":result.tables,"table_cells":result.cells}),
                    )?,
                )?;
            }
        }
        Command::Check => {
            let cancel = Arc::new(AtomicBool::new(false));
            let engine =
                ittm_ocr_runtime::engine::Tesseract::new(limits.command_timeout, cancel.clone())?;
            for name in ["pdfinfo", "pdftotext", "pdftoppm"] {
                document::command_output(
                    std::process::Command::new(name).arg("-v"),
                    limits.command_timeout,
                    &cancel,
                )?;
            }
            println!(
                "{}",
                serde_json::json!({"ready":true,"pipeline_core_abi":ABI_VERSION,"languages":engine.languages})
            );
        }
    }
    Ok(())
}
fn write_checkpoint(output: &std::path::Path, checkpoint: &Checkpoint) -> Result<()> {
    let mut file = tempfile::NamedTempFile::new_in(output)?;
    serde_json::to_writer_pretty(&mut file, checkpoint)?;
    file.write_all(b"\n")?;
    file.persist(output.join("checkpoint.json"))?;
    Ok(())
}
async fn shutdown() {
    #[cfg(unix)]
    {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                .expect("SIGTERM handler");
        tokio::select! {_=tokio::signal::ctrl_c()=>{},_=terminate.recv()=>{}}
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}
