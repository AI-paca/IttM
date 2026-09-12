use crate::{
    document::{self, Limits},
    engine::{EasyOcr, Recognition, Tesseract},
};
use anyhow::{Context, Result, ensure};
use ittm_pipeline_core::{
    ImagePlane,
    session::{STAGES, Session},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Instant,
};

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Profile {
    pub name: String,
    pub psm: [i32; 3],
    pub structural_output: String,
    pub flags: BTreeSet<String>,
}
#[derive(Clone, Debug, Deserialize)]
pub struct Options {
    #[serde(default = "auto", rename = "engine_type")]
    pub engine: String,
    #[serde(default = "auto")]
    pub pdf_mode: String,
    pub pipeline_profile: Option<String>,
    pub pipeline_flags: Option<String>,
}
fn auto() -> String {
    "auto".into()
}
impl Default for Options {
    fn default() -> Self {
        Self {
            engine: auto(),
            pdf_mode: auto(),
            pipeline_profile: None,
            pipeline_flags: None,
        }
    }
}
pub fn catalog() -> Value {
    serde_json::from_str(include_str!("../profiles.json")).expect("Validated profile catalog")
}
impl Options {
    pub fn profile(&self) -> Result<Profile> {
        ensure!(
            ["auto", "tesseract", "easyocr"].contains(&self.engine.as_str()),
            "Unknown OCR engine"
        );
        ensure!(
            ["auto", "raster"].contains(&self.pdf_mode.as_str()),
            "Unknown PDF mode"
        );
        let catalog = catalog();
        let default = format!("backend_{}_standard", self.engine);
        let name = self.pipeline_profile.as_deref().unwrap_or(&default);
        let mut profile: Profile = serde_json::from_value(
            catalog["profiles"]
                .get(name)
                .with_context(|| format!("Unknown OCR pipeline profile: {name}"))?
                .clone(),
        )?;
        if let Some(flags) = &self.pipeline_flags {
            for part in flags
                .split([',', ';'])
                .map(str::trim)
                .filter(|p| !p.is_empty())
            {
                let (key, value) = part
                    .split_once(['=', ':'])
                    .context("Use key:value or key=value for pipeline flags")?;
                let (key, value) = (key.trim(), value.trim());
                let spec = catalog["api"]["supported_overrides"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|s| s["key"] == key)
                    .context("Unknown pipeline flag")?;
                ensure!(
                    spec["modes"].as_array().unwrap().iter().any(|m| m == value),
                    "Unknown {key} mode: {value}"
                );
                profile.flags.retain(|f| !f.starts_with(&format!("{key}:")));
                profile.flags.insert(format!("{key}:{value}"));
                if key == "structural_output" {
                    profile.structural_output = value.into();
                }
                if key == "lexical_correction" {
                    profile
                        .flags
                        .retain(|f| !f.starts_with("ocr_language_retry:"));
                    profile.flags.insert(format!("ocr_language_retry:{value}"));
                }
            }
        }
        Ok(profile)
    }
}

pub struct Engines {
    tesseract: Option<Tesseract>,
    easyocr: Option<EasyOcr>,
    cancel: Arc<AtomicBool>,
    limits: Limits,
}
impl Engines {
    pub fn new(limits: &Limits, cancel: Arc<AtomicBool>) -> Self {
        Self {
            tesseract: None,
            easyocr: None,
            cancel,
            limits: limits.clone(),
        }
    }
    pub fn recognize(
        &mut self,
        plane: &ImagePlane,
        languages: &str,
        psm: i32,
        engine: &str,
    ) -> Result<Recognition> {
        if engine == "easyocr" {
            if self.easyocr.is_none() {
                self.easyocr = Some(EasyOcr::start(
                    self.limits.command_timeout,
                    self.cancel.clone(),
                )?);
            }
            return self.easyocr.as_mut().unwrap().recognize(plane);
        }
        if self.tesseract.is_none() {
            self.tesseract = Some(Tesseract::new(
                self.limits.command_timeout,
                self.cancel.clone(),
            )?);
        }
        self.tesseract
            .as_mut()
            .unwrap()
            .recognize(plane, languages, psm)
    }
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Attempt {
    pub index: u32,
    pub fields: [i32; 18],
    pub languages: String,
    pub result: Recognition,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct Checkpoint {
    pub abi: u32,
    pub route: u32,
    pub width: u32,
    pub height: u32,
    pub profile: Profile,
    pub engine: String,
    pub attempts: Vec<Attempt>,
}
#[derive(Default)]
pub struct PageResult {
    pub markdown: String,
    pub chunks: usize,
    pub tables: usize,
    pub cells: usize,
}

pub fn raster(
    plane: &ImagePlane,
    profile: &Profile,
    engine: &str,
    engines: &mut Engines,
    cancel: &AtomicBool,
    replay: &[Attempt],
    mut observe: impl FnMut(&Attempt, &ImagePlane) -> Result<()>,
) -> Result<PageResult> {
    let mut session = Session::plan(plane).map_err(anyhow::Error::msg)?;
    session.start_ocr().map_err(anyhow::Error::msg)?;
    let mut index = 0;
    let mut tables = BTreeMap::<i32, usize>::new();
    while index < session.job_count() {
        ensure!(!cancel.load(Ordering::Relaxed), "Conversion cancelled");
        let job = session.job(index).map_err(anyhow::Error::msg)?;
        let pixels = session.raster(index).map_err(anyhow::Error::msg)?;
        let result = if let Some(saved) = replay.get(index as usize) {
            ensure!(
                saved.index == index
                    && saved.fields == job.fields
                    && saved.languages == job.languages,
                "Checkpoint no longer matches the shared Rust pipeline"
            );
            saved.result.clone()
        } else {
            engines.recognize(
                &pixels,
                job.languages,
                *profile
                    .psm
                    .get(job.fields[9] as usize)
                    .unwrap_or(&profile.psm[0]),
                engine,
            )?
        };
        let words = result
            .words
            .iter()
            .map(|w| ittm_pipeline_core::session::Word {
                text: w.text.clone(),
                bbox: w.bbox,
                confidence_ppm: w.confidence_ppm,
            })
            .collect::<Vec<_>>();
        session
            .submit(index, &result.text, &words)
            .map_err(anyhow::Error::msg)?;
        if job.fields[10] == 2 {
            let cells = (job.fields[14].max(0) as usize) * (job.fields[15].max(0) as usize);
            tables
                .entry(job.fields[4])
                .and_modify(|v| *v = (*v).max(cells))
                .or_insert(cells);
        }
        observe(
            &Attempt {
                index,
                fields: job.fields,
                languages: job.languages.into(),
                result,
            },
            &pixels,
        )?;
        index += 1;
    }
    ensure!(
        replay.len() <= index as usize,
        "Checkpoint contains extra OCR attempts"
    );
    let markdown = session.render().map_err(anyhow::Error::msg)?;
    ensure!(
        session.stage_mask() == 255,
        "Pipeline did not complete all eight stages"
    );
    Ok(PageResult {
        markdown,
        chunks: index as usize,
        tables: tables.len(),
        cells: tables.values().sum(),
    })
}

#[derive(Debug, Serialize, Deserialize)]
pub struct Conversion {
    pub markdown: String,
    pub meta: Value,
}
pub fn convert_file(
    path: &Path,
    filename: &str,
    options: &Options,
    limits: &Limits,
    cancel: Arc<AtomicBool>,
    mut emit: impl FnMut(Value) -> Result<()>,
) -> Result<Conversion> {
    let start = Instant::now();
    let profile = options.profile()?;
    document::validate_file(path, limits)?;
    let pdf = document::is_pdf(path, filename)?;
    let pages = if pdf {
        document::page_count(path, limits, &cancel)?
    } else {
        1
    };
    let mut engines = Engines::new(limits, cancel.clone());
    let mut parts = Vec::new();
    let mut empty = Vec::new();
    let (mut chunks, mut tables, mut cells, mut text_pages) = (0, 0, 0, 0);
    for page in 1..=pages {
        ensure!(!cancel.load(Ordering::Relaxed), "Conversion cancelled");
        let text = if pdf && options.pdf_mode == "auto" {
            document::text_page(
                path,
                page,
                profile.structural_output != "markdown",
                limits,
                &cancel,
            )
            .unwrap_or(None)
        } else {
            None
        };
        ensure!(!cancel.load(Ordering::Relaxed), "Conversion cancelled");
        emit(
            json!({"type":"progress","stage":if text.is_some(){"pdf_text_layer"}else{"ocr"},"page":page,"total_pages":pages,"percent":0}),
        )?;
        let result = if let Some(text) = text {
            text_pages += 1;
            PageResult {
                markdown: text.markdown,
                tables: text.tables,
                cells: text.cells,
                ..Default::default()
            }
        } else {
            let plane = if pdf {
                document::render_page(path, page, limits, &cancel)?
            } else {
                document::decode(path, limits)?
            };
            raster(
                &plane,
                &profile,
                &options.engine,
                &mut engines,
                &cancel,
                &[],
                |_, _| Ok(()),
            )?
        };
        chunks += result.chunks;
        tables += result.tables;
        cells += result.cells;
        if result.markdown.trim().is_empty() {
            empty.push(page);
            emit(json!({"type":"warning","page":page,"message":"No text recognized on page"}))?;
        }
        emit(json!({"type":"page","page":page,"total_pages":pages,"markdown":result.markdown}))?;
        parts.push(result.markdown);
    }
    let all_text = text_pages == pages;
    let mut flags = profile.flags;
    if !all_text {
        flags.insert("pipeline:rust_separated_v1".into());
        flags.extend(STAGES.iter().map(|s| format!("pipeline_stage:{s}")));
    }
    if text_pages > 0 {
        flags.insert(
            if all_text {
                "pdf_text_layer:all_pages"
            } else {
                "pdf_text_layer:hybrid"
            }
            .into(),
        );
    }
    let meta = json!({"engine":if all_text{"pdf_text_layer"}else if options.engine=="auto"{"tesseract"}else{&options.engine},
        "engine_chain":[if all_text{"pdf_text_layer"}else if options.engine=="auto"{"tesseract"}else{&options.engine}],
        "chunks":chunks,"cards_found":0,"tables_found":tables,"table_cells":cells,"pages":pages,"empty_pages":empty,
        "pipeline":if all_text{"pdf_text_layer"}else if text_pages>0{"pdf_text_layer+rust_separated_v1"}else{"rust_separated_v1"},
        "pipeline_profile":profile.name,"pipeline_stages":if all_text{vec![]}else{STAGES.to_vec()},"pdf_mode":options.pdf_mode,
        "flags":flags,"preprocess_steps":if all_text{vec![]}else{vec!["rust:preprocess"]},"layout_steps":[],
        "pdf_text_layer_pages":text_pages,"elapsed_ms":start.elapsed().as_millis()});
    emit(json!({"type":"complete","meta":meta}))?;
    Ok(Conversion {
        markdown: parts
            .into_iter()
            .filter(|p| !p.trim().is_empty())
            .collect::<Vec<_>>()
            .join("\n\n"),
        meta,
    })
}
