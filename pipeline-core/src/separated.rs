use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::slice;
use std::str;
use std::sync::{Mutex, OnceLock};
use crate::raster::RasterPixels;

use crate::language::{
    LanguageAgenda, LanguageAgendaResult, LanguageObservation, LanguageProfileId, LanguageRequest,
    LanguageSplayState, OcrTransform, gamma_dark_rgb, normalize_dark_small_text_rgb,
};
use crate::{blocks, compact, geometry, grammar, objects, topology};

const ALL_STAGE_MASK: u32 = (1 << 8) - 1;
const PLANNED_STAGE_MASK: u32 = (1 << 5) - 1;
const MAX_JOBS: usize = 4_096;
const OBJECT_PARAGRAPH: u32 = 0;
const OBJECT_LIST: u32 = 1;
const OBJECT_TABLE: u32 = 2;
const OBJECT_UNKNOWN: u32 = 3;
const OBJECT_FLOW: u32 = 4;
const SPLIT_CALIBRATION_SAMPLES: usize = 3;
const SPLIT_CALIBRATION_MIN_SUCCESSES: usize = 2;
#[allow(dead_code)] // Explicit context policy, not canonical OCR preparation.
const OCR_CONTEXT_BORDER: u32 = 8;
const OCR_PARAGRAPH_UPSCALE_MIN_HEIGHT: usize = 512;
const OCR_PARAGRAPH_UPSCALE_MAX_FACTOR: usize = 8;
const OCR_TABLE_UPSCALE_MIN_HEIGHT: usize = 96;
const OCR_TABLE_UPSCALE_MAX_FACTOR: usize = 4;
const OCR_UPSCALE_MAX_PIXELS: usize = 16_000_000;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct Rect {
    left: u32,
    top: u32,
    right: u32,
    bottom: u32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct SegmentCell {
    row: u32,
    column: u32,
    row_span: u32,
    column_span: u32,
}

// A logical matrix slot exists independently of its optional geometric sources.
#[derive(Clone, Debug, Eq, PartialEq)]
struct MatrixCell {
    cell: SegmentCell,
    rect: Rect,
    source_segment_indexes: Vec<usize>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct OcrWordEvidence {
    text: String,
    rect: Rect,
    confidence_milli: u32,
    ranking_confidence_units: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct OcrJob {
    rect: Rect,
    segments: Vec<Rect>,
    source_segment_indexes: Vec<usize>,
    segment_cells: Vec<SegmentCell>,
    compact_segments: bool,
    object_id: u32,
    row: u32,
    column: u32,
    row_span: u32,
    column_span: u32,
    recognition_mode: u32,
    object_kind: u32,
    depth: u32,
    language_profile: LanguageProfileId,
    transform: OcrTransform,
    logical_row_count: u32,
    logical_column_count: u32,
    ocr_required: bool,
    words_in_source_space: bool,
    imported_composite: bool,
}

#[derive(Clone, Debug)]
struct JobRaster {
    width: u32,
    height: u32,
    stride: u32,
    pixels: RasterPixels,
    ocr_scale: u32,
    placements: Vec<RasterPlacement>,
}

#[derive(Clone, Debug)]
struct RasterPlacement {
    segment_indexes: Vec<usize>,
    source_rect: Rect,
    crop_rect: Rect,
}

#[derive(Clone, Debug)]
struct PendingContext {
    job: OcrJob,
    raster: JobRaster,
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct SplitStrategyKey {
    locked_profile: Option<LanguageProfileId>,
    evidenced_profiles: Vec<LanguageProfileId>,
}

#[derive(Clone, Copy, Debug, Default)]
struct SplitCalibration {
    samples: usize,
    successes: usize,
    ineffective: bool,
}

#[derive(Debug)]
struct TableSplitProbe {
    calibration_key: Option<SplitStrategyKey>,
    root_winner: usize,
    descendant_start: usize,
    baseline_grammar_milli: u32,
    baseline_mean_confidence_milli: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct RecognizedSegment {
    object_id: u32,
    object_kind: u32,
    source_segment_indexes: Vec<usize>,
    cell: SegmentCell,
    text: String,
    evidence_job_indexes: Vec<usize>,
}

#[derive(Debug)]
struct Session {
    objects: Vec<objects::DocumentObject>,
    blocks: Vec<blocks::RecognitionBlock>,
    block_rasters: Vec<JobRaster>,
    jobs: Vec<OcrJob>,
    job_rasters: Vec<JobRaster>,
    text: Vec<Option<String>>,
    confidence_milli: Vec<u32>,
    mean_word_confidence_milli: Vec<u32>,
    words: Vec<Vec<OcrWordEvidence>>,
    superseded: Vec<bool>,
    selected: Vec<bool>,
    pending_contexts: VecDeque<PendingContext>,
    active_context: Option<PendingContext>,
    active_agenda: Option<LanguageAgenda>,
    active_attempts: Vec<usize>,
    language_state: LanguageSplayState,
    evidenced_native_profiles: BTreeSet<LanguageProfileId>,
    table_profile_discovery_complete: bool,
    split_calibrations: BTreeMap<SplitStrategyKey, SplitCalibration>,
    active_split_probe: Option<TableSplitProbe>,
    recognized_segments: Option<Vec<RecognizedSegment>>,
    recognized_layouts: BTreeMap<u32, (u32, u32, u32)>,
    matrix_cells: BTreeMap<u32, Vec<MatrixCell>>,
    reading_order_objects: BTreeSet<u32>,
    importing_ocr: bool,
    topology: topology::PhysicalTopology,
    rendered: Option<Vec<u8>>,
    stage_mask: u32,
}

struct ImportCursor<'a> {
    values: &'a [u32],
    index: usize,
}

impl<'a> ImportCursor<'a> {
    fn new(values: &'a [u32]) -> Self {
        Self { values, index: 0 }
    }

    fn take(&mut self) -> Option<u32> {
        let value = *self.values.get(self.index)?;
        self.index += 1;
        Some(value)
    }

    fn rect(&mut self) -> Option<Rect> {
        Some(Rect {
            left: self.take()?,
            top: self.take()?,
            right: self.take()?,
            bottom: self.take()?,
        })
    }

    fn indexes(&mut self) -> Option<Vec<usize>> {
        let count = self.take()? as usize;
        (0..count).map(|_| self.take().map(|value| value as usize)).collect()
    }

    fn finished(&self) -> bool {
        self.index == self.values.len()
    }
}

fn raster_for_request(
    raw: &JobRaster,
    request: LanguageRequest,
    object_kind: u32,
) -> JobRaster {
    let grayscale = |source: &JobRaster, gray: Vec<u8>| {
        let mut pixels = Vec::with_capacity(gray.len().saturating_mul(3));
        for value in gray {
            pixels.extend_from_slice(&[value, value, value]);
        }
        JobRaster {
            width: source.width,
            height: source.height,
            stride: source.width.saturating_mul(3),
            pixels: pixels.into(),
            ocr_scale: 1,
            placements: source.placements.clone(),
        }
    };
    let transformed = if request.transform == OcrTransform::Raw {
        raw.clone()
    } else if let Some(gray) = gamma_dark_rgb(&raw.pixels.decoded(), 3) {
        grayscale(raw, gray)
    } else {
        raw.clone()
    };
    if let Some(gray) = normalize_dark_small_text_rgb(
        &transformed.pixels.decoded(),
        transformed.width as usize,
        transformed.height as usize,
    ) {
        // Python returns normalized dark text at its original geometry before
        // considering height-based enlargement.
        return grayscale(&transformed, gray);
    }
    // The frozen adapter adds a border only for recognition-miss retries.
    // Ordinary attempts preserve the canonical crop's border and coordinates.
    let (minimum_height, maximum_factor) = if object_kind == OBJECT_TABLE {
        (
            OCR_TABLE_UPSCALE_MIN_HEIGHT,
            OCR_TABLE_UPSCALE_MAX_FACTOR,
        )
    } else {
        (
            OCR_PARAGRAPH_UPSCALE_MIN_HEIGHT,
            OCR_PARAGRAPH_UPSCALE_MAX_FACTOR,
        )
    };
    let width = transformed.width as usize;
    let height = transformed.height as usize;
    let mut scale = minimum_height
        .div_ceil(height.max(1))
        .clamp(1, maximum_factor);
    while scale > 1
        && width
            .checked_mul(height)
            .and_then(|pixels| pixels.checked_mul(scale))
            .and_then(|pixels| pixels.checked_mul(scale))
            .is_none_or(|pixels| pixels > OCR_UPSCALE_MAX_PIXELS)
    {
        scale -= 1;
    }
    if scale == 1 {
        return transformed;
    }
    let scale_u32 = scale as u32;
    JobRaster {
        width: transformed.width.saturating_mul(scale_u32),
        height: transformed.height.saturating_mul(scale_u32),
        stride: transformed.stride.saturating_mul(scale_u32),
        pixels: compact::resize_lanczos(&transformed.pixels.decoded(), width, height, scale).into(),
        ocr_scale: scale_u32,
        placements: transformed
            .placements
            .iter()
            .map(|placement| RasterPlacement {
                segment_indexes: placement.segment_indexes.clone(),
                source_rect: placement.source_rect,
                crop_rect: Rect {
                    left: placement.crop_rect.left.saturating_mul(scale_u32),
                    top: placement.crop_rect.top.saturating_mul(scale_u32),
                    right: placement.crop_rect.right.saturating_mul(scale_u32),
                    bottom: placement.crop_rect.bottom.saturating_mul(scale_u32),
                },
            })
            .collect(),
    }
}

#[allow(dead_code)] // Kept for callers explicitly requesting context padding.
fn add_ocr_context_border(source: JobRaster) -> JobRaster {
    let Some(width) = source.width.checked_add(OCR_CONTEXT_BORDER.saturating_mul(2)) else {
        return source;
    };
    let Some(height) = source.height.checked_add(OCR_CONTEXT_BORDER.saturating_mul(2)) else {
        return source;
    };
    let Some(stride) = width.checked_mul(3) else {
        return source;
    };
    let Some(pixel_length) = stride.checked_mul(height).map(|value| value as usize) else {
        return source;
    };
    let mut pixels = vec![255_u8; pixel_length];
    let source_pixels = source.pixels.decoded();
    let source_row_length = source.width.saturating_mul(3) as usize;
    for row in 0..source.height as usize {
        let source_start = row.saturating_mul(source.stride as usize);
        let source_stop = source_start.saturating_add(source_row_length);
        let target_start = (row + OCR_CONTEXT_BORDER as usize)
            .saturating_mul(stride as usize)
            .saturating_add(OCR_CONTEXT_BORDER as usize * 3);
        let target_stop = target_start.saturating_add(source_row_length);
        let (Some(source_row), Some(target_row)) = (
            source_pixels.get(source_start..source_stop),
            pixels.get_mut(target_start..target_stop),
        ) else {
            return source;
        };
        target_row.copy_from_slice(source_row);
    }
    JobRaster {
        width,
        height,
        stride,
        pixels: pixels.into(),
        ocr_scale: 1,
        placements: source
            .placements
            .iter()
            .map(|placement| RasterPlacement {
                segment_indexes: placement.segment_indexes.clone(),
                source_rect: placement.source_rect,
                crop_rect: Rect {
                    left: placement.crop_rect.left.saturating_add(OCR_CONTEXT_BORDER),
                    top: placement.crop_rect.top.saturating_add(OCR_CONTEXT_BORDER),
                    right: placement.crop_rect.right.saturating_add(OCR_CONTEXT_BORDER),
                    bottom: placement.crop_rect.bottom.saturating_add(OCR_CONTEXT_BORDER),
                },
            })
            .collect(),
    }
}

fn append_attempt(session: &mut Session, request: LanguageRequest) -> bool {
    if session.jobs.len() >= MAX_JOBS {
        return false;
    }
    let Some(context) = session.active_context.as_ref() else {
        return false;
    };
    let mut job = context.job.clone();
    job.language_profile = request.profile;
    job.transform = request.transform;
    let raster = session
        .active_attempts
        .iter()
        .copied()
        .find(|index| session.jobs[*index].transform == request.transform)
        .and_then(|index| session.job_rasters.get(index))
        .filter(|raster| !raster.pixels.is_empty())
        .cloned()
        .unwrap_or_else(|| raster_for_request(&context.raster, request, job.object_kind));
    let index = session.jobs.len();
    session.jobs.push(job);
    session.job_rasters.push(raster);
    session.text.push(None);
    session.confidence_milli.push(0);
    session.mean_word_confidence_milli.push(0);
    session.words.push(Vec::new());
    session.superseded.push(false);
    session.selected.push(false);
    session.active_attempts.push(index);
    true
}

fn release_active_raster_pixels(session: &mut Session) {
    for index in session.active_attempts.iter().copied() {
        if let Some(raster) = session.job_rasters.get_mut(index) {
            raster.pixels = RasterPixels::default();
        }
    }
}

fn finalize_table_split_probe(session: &mut Session) {
    let Some(probe) = session.active_split_probe.take() else {
        return;
    };
    let retained = (probe.descendant_start..session.jobs.len())
        .filter(|index| session.selected[*index] && !session.superseded[*index])
        .collect::<Vec<_>>();
    let total_characters = retained
        .iter()
        .map(|index| {
            session.text[*index]
                .as_deref()
                .unwrap_or_default()
                .chars()
                .count()
                .max(1)
        })
        .sum::<usize>();
    let composite_grammar_milli = if total_characters == 0 {
        0
    } else {
        retained
            .iter()
            .map(|index| {
                session.confidence_milli[*index] as usize
                    * session.text[*index]
                        .as_deref()
                        .unwrap_or_default()
                        .chars()
                        .count()
                        .max(1)
            })
            .sum::<usize>()
            .div_ceil(total_characters) as u32
    };
    let word_count = retained
        .iter()
        .map(|index| session.words[*index].len())
        .sum::<usize>();
    let composite_mean_confidence_milli = if word_count == 0 {
        0
    } else {
        retained
            .iter()
            .flat_map(|index| session.words[*index].iter())
            .map(|word| word.confidence_milli as usize)
            .sum::<usize>()
            .div_ceil(word_count) as u32
    };
    let has_text = retained.iter().any(|index| {
        session.text[*index]
            .as_deref()
            .is_some_and(|text| !text.trim().is_empty())
    });
    let acceptable = has_text
        && (composite_grammar_milli >= 970 || composite_mean_confidence_milli >= 850);
    let improved = acceptable
        && (composite_grammar_milli, composite_mean_confidence_milli)
            > (
                probe.baseline_grammar_milli,
                probe.baseline_mean_confidence_milli,
            );
    if !improved {
        for index in probe.descendant_start..session.jobs.len() {
            session.superseded[index] = true;
        }
        session.superseded[probe.root_winner] = false;
        session.selected[probe.root_winner] = true;
    }
    if let Some(key) = probe.calibration_key {
        let calibration = session.split_calibrations.entry(key).or_default();
        calibration.samples += 1;
        calibration.successes += usize::from(improved);
        if calibration.samples >= SPLIT_CALIBRATION_SAMPLES
            && calibration.successes < SPLIT_CALIBRATION_MIN_SUCCESSES
        {
            calibration.ineffective = true;
        }
    }
}

fn start_next_context(session: &mut Session) {
    if session.active_split_probe.is_some()
        && session
            .pending_contexts
            .front()
            .is_none_or(|context| context.job.depth == 0)
    {
        finalize_table_split_probe(session);
    }
    let Some(context) = session.pending_contexts.pop_front() else {
        session.active_context = None;
        session.active_agenda = None;
        session.active_attempts.clear();
        session.stage_mask |= 1 << 5;
        return;
    };
    let evidenced_profiles = session
        .evidenced_native_profiles
        .iter()
        .copied()
        .collect::<Vec<_>>();
    let table_context = context.job.object_kind == OBJECT_TABLE;
    let agenda = if table_context
        && !session.table_profile_discovery_complete
        && session.language_state.locked_profile.is_some()
    {
        LanguageAgenda::begin_locked_profile(&session.language_state)
    } else {
        LanguageAgenda::begin_for_context_with_fallbacks(
            &session.language_state,
            table_context,
            &evidenced_profiles,
        )
    };
    let request = agenda.request();
    session.active_context = Some(context);
    session.active_agenda = Some(agenda);
    session.active_attempts.clear();
    let _ = append_attempt(session, request);
}

fn mean_ranking_confidence(words: &[OcrWordEvidence]) -> u64 {
    words
        .iter()
        .map(|word| u64::from(word.ranking_confidence_units))
        .sum::<u64>()
        .checked_div(words.len() as u64)
        .unwrap_or(0)
}

fn fuse_word_slots(
    attempts: &[(usize, u32, Vec<OcrWordEvidence>)],
) -> Option<(usize, Vec<(Rect, Vec<OcrWordEvidence>, usize)>)> {
    let (baseline_index, _grammar, baseline_words) = attempts
        .iter()
        .filter(|(_index, _grammar, words)| !words.is_empty())
        .max_by_key(|(index, grammar, words)| {
            (
                mean_ranking_confidence(words),
                *grammar,
                usize::MAX.saturating_sub(*index),
            )
        })?;
    let mut candidates =
        vec![Vec::<(usize, u64, Vec<OcrWordEvidence>)>::new(); baseline_words.len()];
    for (attempt_index, _grammar, words) in attempts {
        let attempt_confidence = mean_ranking_confidence(words);
        let mut assigned = vec![Vec::<OcrWordEvidence>::new(); baseline_words.len()];
        for word in words {
            let word_center_x = u64::from(word.rect.left) + u64::from(word.rect.right);
            let word_center_y = u64::from(word.rect.top) + u64::from(word.rect.bottom);
            let Some((slot_index, _slot)) = baseline_words
                .iter()
                .enumerate()
                .filter_map(|(slot_index, slot)| {
                    let area = intersection_area(word.rect, slot.rect);
                    if area == 0 {
                        return None;
                    }
                    let slot_center_x = u64::from(slot.rect.left) + u64::from(slot.rect.right);
                    let slot_center_y = u64::from(slot.rect.top) + u64::from(slot.rect.bottom);
                    let distance = word_center_x
                        .abs_diff(slot_center_x)
                        .saturating_add(word_center_y.abs_diff(slot_center_y));
                    Some((
                        slot_index,
                        slot,
                        (
                            area,
                            u64::MAX.saturating_sub(distance),
                            usize::MAX.saturating_sub(slot_index),
                        ),
                    ))
                })
                .max_by_key(|(_slot_index, _slot, score)| *score)
                .map(|(slot_index, slot, _score)| (slot_index, slot))
            else {
                continue;
            };
            assigned[slot_index].push(word.clone());
        }
        for (slot_index, words) in assigned.into_iter().enumerate() {
            if !words.is_empty() {
                candidates[slot_index].push((*attempt_index, attempt_confidence, words));
            }
        }
    }
    let slots = baseline_words
        .iter()
        .zip(candidates)
        .filter_map(|(baseline_word, candidates)| {
            candidates
                .into_iter()
                .max_by_key(|(attempt_index, attempt_confidence, words)| {
                    let slot_confidence = mean_ranking_confidence(words);
                    (
                        slot_confidence.saturating_add(*attempt_confidence),
                        slot_confidence,
                        *attempt_confidence,
                        usize::MAX.saturating_sub(*attempt_index),
                    )
                })
                .map(|(attempt_index, _attempt_confidence, words)| {
                    (baseline_word.rect, words, attempt_index)
                })
        })
        .collect::<Vec<_>>();
    (!slots.is_empty()).then_some((*baseline_index, slots))
}

fn render_fused_word_slots(slots: &[(Rect, Vec<OcrWordEvidence>, usize)]) -> String {
    let mut output = String::new();
    let mut previous_rect = None::<Rect>;
    for (rect, words, _attempt_index) in slots {
        if !output.is_empty() {
            output.push(if previous_rect.is_some_and(|previous| rect.top > previous.bottom) {
                '\n'
            } else {
                ' '
            });
        }
        output.push_str(
            &words
                .iter()
                .map(|word| word.text.as_str())
                .collect::<Vec<_>>()
                .join(" "),
        );
        previous_rect = Some(*rect);
    }
    output
}

fn fuse_active_word_evidence(session: &mut Session) -> Option<LanguageRequest> {
    let attempts = session
        .active_attempts
        .iter()
        .copied()
        .map(|index| {
            (
                index,
                session.confidence_milli[index],
                session.words[index].clone(),
            )
        })
        .collect::<Vec<_>>();
    let (baseline_index, slots) = fuse_word_slots(&attempts)?;
    let contributors = slots
        .iter()
        .map(|(_rect, _words, attempt_index)| *attempt_index)
        .collect::<BTreeSet<_>>();
    if contributors.len() < 2 {
        return None;
    }
    let text = render_fused_word_slots(&slots);
    if text.trim().is_empty() {
        return None;
    }
    let words = slots
        .into_iter()
        .flat_map(|(_rect, words, _attempt_index)| words)
        .collect::<Vec<_>>();
    let word_confidences = words
        .iter()
        .map(|word| f64::from(word.ranking_confidence_units) / 1_000_000.0)
        .collect::<Vec<_>>();
    let assessment = grammar::assess_grammar(
        &text,
        session.jobs[baseline_index].language_profile.code(),
        &word_confidences,
    );
    session.text[baseline_index] = Some(text);
    session.words[baseline_index] = words;
    session.confidence_milli[baseline_index] = u32::from(assessment.percent) * 10;
    session.mean_word_confidence_milli[baseline_index] = if word_confidences.is_empty() {
        0
    } else {
        (word_confidences.iter().copied().sum::<f64>() / word_confidences.len() as f64
            * 1_000.0)
            .round_ties_even() as u32
    };
    Some(LanguageRequest {
        profile: session.jobs[baseline_index].language_profile,
        transform: session.jobs[baseline_index].transform,
    })
}

fn select_active_winner(session: &mut Session, request: LanguageRequest) {
    let winner = session
        .active_attempts
        .iter()
        .copied()
        .find(|index| {
            let job = &session.jobs[*index];
            job.language_profile == request.profile && job.transform == request.transform
        })
        .or_else(|| session.active_attempts.first().copied());
    for index in session.active_attempts.iter().copied() {
        let selected = Some(index) == winner;
        session.selected[index] = selected;
        session.superseded[index] = !selected;
    }
}

fn project_source_rect_to_crop(rect: Rect, placement: &RasterPlacement) -> Rect {
    let source_width = placement.source_rect.right - placement.source_rect.left;
    let source_height = placement.source_rect.bottom - placement.source_rect.top;
    let crop_width = placement.crop_rect.right - placement.crop_rect.left;
    let crop_height = placement.crop_rect.bottom - placement.crop_rect.top;
    let project_x = |value: u32| {
        let local = value
            .clamp(placement.source_rect.left, placement.source_rect.right)
            .saturating_sub(placement.source_rect.left);
        placement.crop_rect.left.saturating_add(
            (f64::from(local) * f64::from(crop_width) / f64::from(source_width.max(1)))
                .round_ties_even() as u32,
        )
    };
    let project_y = |value: u32| {
        let local = value
            .clamp(placement.source_rect.top, placement.source_rect.bottom)
            .saturating_sub(placement.source_rect.top);
        placement.crop_rect.top.saturating_add(
            (f64::from(local) * f64::from(crop_height) / f64::from(source_height.max(1)))
                .round_ties_even() as u32,
        )
    };
    let left = project_x(rect.left).min(placement.crop_rect.right.saturating_sub(1));
    let top = project_y(rect.top).min(placement.crop_rect.bottom.saturating_sub(1));
    Rect {
        left,
        top,
        right: project_x(rect.right).clamp(left.saturating_add(1), placement.crop_rect.right),
        bottom: project_y(rect.bottom).clamp(top.saturating_add(1), placement.crop_rect.bottom),
    }
}

fn recursive_raster_placements(job: &OcrJob, raster: &JobRaster) -> Vec<RasterPlacement> {
    raster
        .placements
        .iter()
        .flat_map(|placement| {
            if placement.segment_indexes.len() <= 1 {
                return vec![placement.clone()];
            }
            placement
                .segment_indexes
                .iter()
                .filter_map(|index| {
                    let source_rect = job.segments.get(*index).copied()?;
                    Some(RasterPlacement {
                        segment_indexes: vec![*index],
                        source_rect,
                        crop_rect: project_source_rect_to_crop(source_rect, placement),
                    })
                })
                .collect()
        })
        .collect()
}

fn repack_parent_placements(
    parent: &JobRaster,
    selected: &[RasterPlacement],
    parent_to_child: &BTreeMap<usize, usize>,
) -> Option<JobRaster> {
    if selected.is_empty() || parent.stride as usize != parent.width as usize * 3 {
        return None;
    }
    let tile_sizes = selected
        .iter()
        .map(|placement| {
            if placement.crop_rect.right > parent.width
                || placement.crop_rect.bottom > parent.height
                || placement.crop_rect.left >= placement.crop_rect.right
                || placement.crop_rect.top >= placement.crop_rect.bottom
            {
                return None;
            }
            Some((
                (placement.crop_rect.right - placement.crop_rect.left) as usize,
                (placement.crop_rect.bottom - placement.crop_rect.top) as usize,
            ))
        })
        .collect::<Option<Vec<_>>>()?;
    let total_pixels = tile_sizes
        .iter()
        .try_fold(0_usize, |total, (width, height)| {
            total.checked_add(width.checked_mul(*height)?)
        })?;
    let maximum_width = tile_sizes.iter().map(|(width, _)| *width).max()?;
    let target_width = maximum_width.max((total_pixels as f64).sqrt().ceil() as usize);
    let gap = 8_usize;
    let mut positions = Vec::with_capacity(selected.len());
    let mut left = 0_usize;
    let mut top = 0_usize;
    let mut row_height = 0_usize;
    let mut atlas_width = 0_usize;
    for (width, height) in tile_sizes.iter().copied() {
        if left > 0 && left.saturating_add(gap).saturating_add(width) > target_width {
            top = top.saturating_add(row_height).saturating_add(gap);
            left = 0;
            row_height = 0;
        }
        positions.push((left, top));
        atlas_width = atlas_width.max(left.saturating_add(width));
        left = left.saturating_add(width).saturating_add(gap);
        row_height = row_height.max(height);
    }
    let atlas_height = top.saturating_add(row_height).max(1);
    let atlas_width = atlas_width.max(1);
    let stride = atlas_width.checked_mul(3)?;
    let mut pixels = vec![255_u8; stride.checked_mul(atlas_height)?];
    let mut placements = Vec::with_capacity(selected.len());
    let parent_pixels = parent.pixels.decoded();
    for ((placement, (width, height)), (left, top)) in selected
        .iter()
        .zip(tile_sizes.iter().copied())
        .zip(positions.iter().copied())
    {
        for row in 0..height {
            let source = (placement.crop_rect.top as usize + row)
                .checked_mul(parent.stride as usize)?
                .checked_add(placement.crop_rect.left as usize * 3)?;
            let target = (top + row).checked_mul(stride)?.checked_add(left * 3)?;
            let count = width.checked_mul(3)?;
            pixels[target..target + count].copy_from_slice(&parent_pixels[source..source + count]);
        }
        placements.push(RasterPlacement {
            segment_indexes: placement
                .segment_indexes
                .iter()
                .map(|index| parent_to_child.get(index).copied())
                .collect::<Option<Vec<_>>>()?,
            source_rect: placement.source_rect,
            crop_rect: Rect {
                left: left as u32,
                top: top as u32,
                right: (left + width) as u32,
                bottom: (top + height) as u32,
            },
        });
    }
    Some(JobRaster {
        width: atlas_width as u32,
        height: atlas_height as u32,
        stride: stride as u32,
        pixels: pixels.into(),
        ocr_scale: 1,
        placements,
    })
}

fn compact_context_children(
    parent: &OcrJob,
    parent_raster: &JobRaster,
    chunk_size: usize,
    child_depth: u32,
) -> Option<Vec<PendingContext>> {
    let recursive_placements = recursive_raster_placements(parent, parent_raster);
    if chunk_size == 0 || recursive_placements.len() <= chunk_size {
        return Some(Vec::new());
    }
    let mut children = Vec::new();
    for selected in recursive_placements.chunks(chunk_size) {
        let mut parent_indexes = selected
            .iter()
            .flat_map(|placement| placement.segment_indexes.iter().copied())
            .collect::<Vec<_>>();
        parent_indexes.sort_unstable();
        parent_indexes.dedup();
        let parent_to_child = parent_indexes
            .iter()
            .copied()
            .enumerate()
            .map(|(child, parent)| (parent, child))
            .collect::<BTreeMap<_, _>>();
        let segments = parent_indexes
            .iter()
            .map(|index| parent.segments.get(*index).copied())
            .collect::<Option<Vec<_>>>()?;
        let segment_cells = parent_indexes
            .iter()
            .map(|index| parent.segment_cells.get(*index).copied())
            .collect::<Option<Vec<_>>>()?;
        let source_segment_indexes = parent_indexes
            .iter()
            .map(|index| parent.source_segment_indexes.get(*index).copied())
            .collect::<Option<Vec<_>>>()?;
        let rect = selected
            .iter()
            .map(|placement| placement.source_rect)
            .reduce(|left, right| Rect {
                left: left.left.min(right.left),
                top: left.top.min(right.top),
                right: left.right.max(right.right),
                bottom: left.bottom.max(right.bottom),
            })?;
        let raster = repack_parent_placements(parent_raster, selected, &parent_to_child)?;
        let mut child = parent.clone();
        child.rect = rect;
        child.segments = segments;
        child.source_segment_indexes = source_segment_indexes;
        child.segment_cells = segment_cells;
        child.row = child
            .segment_cells
            .iter()
            .map(|cell| cell.row)
            .min()
            .unwrap_or(parent.row);
        child.column = child
            .segment_cells
            .iter()
            .map(|cell| cell.column)
            .min()
            .unwrap_or(parent.column);
        child.row_span = child
            .segment_cells
            .iter()
            .map(|cell| cell.row.saturating_add(cell.row_span))
            .max()
            .unwrap_or(child.row)
            .saturating_sub(child.row);
        child.column_span = child
            .segment_cells
            .iter()
            .map(|cell| cell.column.saturating_add(cell.column_span))
            .max()
            .unwrap_or(child.column)
            .saturating_sub(child.column);
        child.recognition_mode = recognition_mode_for_rect(rect);
        child.depth = child_depth;
        child.language_profile = LanguageProfileId::RusEng;
        child.transform = OcrTransform::Raw;
        children.push(PendingContext { job: child, raster });
    }
    Some(children)
}

fn character_matches_native_profile(character: char, profile: LanguageProfileId) -> bool {
    let code = character as u32;
    match profile {
        LanguageProfileId::ChiSim => matches!(
            code,
            0x3400..=0x4dbf
                | 0x4e00..=0x9fff
                | 0xf900..=0xfaff
                | 0x3040..=0x30ff
                | 0x31f0..=0x31ff
        ),
        LanguageProfileId::Ell => matches!(code, 0x0370..=0x03ff | 0x1f00..=0x1fff),
        _ => false,
    }
}

fn attempt_native_evidence(
    session: &Session,
    index: usize,
    minimum_density_percent: usize,
) -> Option<(usize, usize, LanguageRequest)> {
    let job = session.jobs.get(index)?;
    if !matches!(
        job.language_profile,
        LanguageProfileId::ChiSim | LanguageProfileId::Ell
    ) {
        return None;
    }
    let text = session.text.get(index)?.as_deref()?;
    let alphanumeric = text
        .chars()
        .filter(|character| character.is_alphanumeric())
        .count();
    let native = text
        .chars()
        .filter(|character| {
            character.is_alphanumeric()
                && character_matches_native_profile(*character, job.language_profile)
        })
        .count();
    let words = session.words.get(index)?;
    let mean_confidence_milli = if words.is_empty() {
        0
    } else {
        words
            .iter()
            .map(|word| usize::try_from(word.confidence_milli).unwrap_or(usize::MAX))
            .sum::<usize>()
            / words.len()
    };
    (native >= 2
        && mean_confidence_milli >= 600
        && native.saturating_mul(100)
            >= alphanumeric.max(1).saturating_mul(minimum_density_percent))
    .then_some((
        native,
        mean_confidence_milli,
        LanguageRequest {
            profile: job.language_profile,
            transform: job.transform,
        },
    ))
}

fn native_request_with_evidence(
    session: &Session,
    minimum_density_percent: usize,
) -> Option<LanguageRequest> {
    session
        .active_attempts
        .iter()
        .filter_map(|index| {
            let job = session.jobs.get(*index)?;
            if !matches!(
                job.language_profile,
                LanguageProfileId::ChiSim | LanguageProfileId::Ell
            ) {
                return None;
            }
            let text = session.text.get(*index)?.as_deref()?;
            let alphanumeric = text
                .chars()
                .filter(|character| character.is_alphanumeric())
                .count();
            let native = text
                .chars()
                .filter(|character| {
                    character.is_alphanumeric()
                        && character_matches_native_profile(*character, job.language_profile)
                })
                .count();
            let words = session.words.get(*index)?;
            let mean_confidence_milli = if words.is_empty() {
                0
            } else {
                words
                    .iter()
                    .map(|word| usize::try_from(word.confidence_milli).unwrap_or(usize::MAX))
                    .sum::<usize>()
                    / words.len()
            };
            (native >= 2
                && mean_confidence_milli >= 600
                && native.saturating_mul(100)
                    >= alphanumeric.max(1).saturating_mul(minimum_density_percent))
            .then_some((
                native,
                mean_confidence_milli,
                LanguageRequest {
                    profile: job.language_profile,
                    transform: job.transform,
                },
            ))
        })
        .max_by_key(|(native, confidence, request)| {
            (
                *native,
                *confidence,
                std::cmp::Reverse(request.profile as u8),
            )
        })
        .map(|(_, _, request)| request)
}

fn winner_has_native_profile(
    session: &Session,
    winner_request: LanguageRequest,
    profile: LanguageProfileId,
) -> bool {
    session.active_attempts.iter().any(|index| {
        let job = &session.jobs[*index];
        job.language_profile == winner_request.profile
            && job.transform == winner_request.transform
            && session.text[*index].as_deref().is_some_and(|text| {
                text.chars()
                    .any(|character| character_matches_native_profile(character, profile))
            })
    })
}

fn rect_intersection_area(first: Rect, second: Rect) -> u64 {
    let width = first
        .right
        .min(second.right)
        .saturating_sub(first.left.max(second.left));
    let height = first
        .bottom
        .min(second.bottom)
        .saturating_sub(first.top.max(second.top));
    u64::from(width) * u64::from(height)
}

fn native_only_word(text: &str, profile: LanguageProfileId) -> bool {
    let mut native = false;
    for character in text.chars() {
        if character_matches_native_profile(character, profile) {
            native = true;
        } else if character.is_alphabetic() {
            return false;
        }
    }
    native
}

fn word_source_projection(word: &OcrWordEvidence, raster: &JobRaster) -> Option<(usize, Rect)> {
    let (placement_index, area) = raster
        .placements
        .iter()
        .enumerate()
        .map(|(index, placement)| {
            (
                index,
                rect_intersection_area(word.rect, placement.crop_rect),
            )
        })
        .max_by_key(|(index, area)| (*area, std::cmp::Reverse(*index)))?;
    (area > 0).then(|| {
        (
            placement_index,
            project_crop_rect_to_source(word.rect, &raster.placements[placement_index]),
        )
    })
}

fn patch_missing_native_units(
    session: &mut Session,
    winner_request: LanguageRequest,
    native_request: LanguageRequest,
) -> bool {
    let Some(winner_index) = session.active_attempts.iter().copied().find(|index| {
        let job = &session.jobs[*index];
        job.language_profile == winner_request.profile && job.transform == winner_request.transform
    }) else {
        return false;
    };
    let Some(native_index) = session.active_attempts.iter().copied().find(|index| {
        let job = &session.jobs[*index];
        job.language_profile == native_request.profile && job.transform == native_request.transform
    }) else {
        return false;
    };
    let Some(winner_raster) = session.job_rasters.get(winner_index).cloned() else {
        return false;
    };
    let Some(native_raster) = session.job_rasters.get(native_index).cloned() else {
        return false;
    };

    let mut replacements = BTreeMap::<usize, Vec<(OcrWordEvidence, Rect)>>::new();
    for word in session.words[native_index].iter().cloned() {
        if !native_only_word(&word.text, native_request.profile) {
            continue;
        }
        let Some((placement_index, source_rect)) =
            word_source_projection(&word, &native_raster)
        else {
            continue;
        };
        replacements
            .entry(placement_index)
            .or_default()
            .push((word, source_rect));
    }
    replacements.retain(|_, words| {
        words
            .iter()
            .flat_map(|(word, _)| word.text.chars())
            .filter(|character| character.is_alphanumeric())
            .count()
            >= 2
    });
    if replacements.is_empty() {
        return false;
    }

    let winner_words = session.words[winner_index].clone();
    let winner_sources = winner_words
        .iter()
        .map(|word| word_source_projection(word, &winner_raster))
        .collect::<Vec<_>>();
    let mut patched_native_words = Vec::<(OcrWordEvidence, Rect, usize)>::new();
    for (placement_index, replacement_words) in replacements {
        for (native, native_source_rect) in replacement_words {
            let matched = winner_sources
                .iter()
                .flatten()
                .any(|(winner_placement, winner_source_rect)| {
                    *winner_placement == placement_index
                        && rect_intersection_area(native_source_rect, *winner_source_rect) > 0
                });
            if matched {
                patched_native_words.push((native, native_source_rect, placement_index));
            }
        }
    }
    if patched_native_words.is_empty() {
        return false;
    }
    let mut words = winner_words
        .into_iter()
        .zip(winner_sources)
        .filter(|(_, winner_source)| {
            let Some((winner_placement, winner_source_rect)) = winner_source else {
                return true;
            };
            !patched_native_words
                .iter()
                .any(|(_, native_source_rect, native_placement)| {
                    native_placement == winner_placement
                        && rect_intersection_area(*winner_source_rect, *native_source_rect) > 0
                })
        })
        .map(|(word, _)| word)
        .collect::<Vec<_>>();
    let mut seen = BTreeSet::new();
    words.extend(
        patched_native_words
            .into_iter()
            .filter_map(|(mut native, source_rect, placement_index)| {
                let placement = winner_raster.placements.get(placement_index)?;
                native.rect = project_source_rect_to_crop(source_rect, placement);
                seen.insert((
                    native.text.clone(),
                    native.rect.left,
                    native.rect.top,
                    native.rect.right,
                    native.rect.bottom,
                ))
                .then_some(native)
            }),
    );
    sort_in_reading_order(&mut words, |word| word.rect);
    let text = words
        .iter()
        .map(|word| word.text.as_str())
        .collect::<Vec<_>>()
        .join(" ");
    let confidences = words
        .iter()
        .map(|word| f64::from(word.ranking_confidence_units) / 1_000_000.0)
        .collect::<Vec<_>>();
    let assessment = grammar::assess_grammar(
        &text,
        session.jobs[winner_index].language_profile.code(),
        &confidences,
    );
    session.words[winner_index] = words;
    session.text[winner_index] = Some(text);
    session.confidence_milli[winner_index] = u32::from(assessment.percent) * 10;
    true
}

fn split_active_context(
    session: &mut Session,
    missing_native_profile: Option<LanguageProfileId>,
) -> bool {
    let Some(context) = session.active_context.as_ref() else {
        return false;
    };
    let unit_count = recursive_raster_placements(&context.job, &context.raster).len();
    let planned_finer_block_exists = context.job.depth == 0
        && session.blocks.iter().any(|block| {
            block.scope_index == context.job.object_id as usize
                && !block.segment_indexes.is_empty()
                && block.segment_indexes.len() < unit_count
        });
    if planned_finer_block_exists {
        return false;
    }
    let Some(chunk_size) = recursive_chunk_size(
        context.job.depth,
        unit_count,
        missing_native_profile.is_some(),
    ) else {
        return false;
    };
    let Some(children) = compact_context_children(
        &context.job,
        &context.raster,
        chunk_size,
        context.job.depth.saturating_add(1),
    ) else {
        return false;
    };
    if children.is_empty() || session.jobs.len().saturating_add(children.len()) > MAX_JOBS {
        return false;
    }
    let root_winner = session
        .active_attempts
        .iter()
        .copied()
        .find(|index| session.selected[*index]);
    let calibration_key = (context.job.depth == 0
        && context.job.object_kind == OBJECT_TABLE
        && missing_native_profile.is_none()
        && session.evidenced_native_profiles.is_empty())
    .then(|| SplitStrategyKey {
        locked_profile: session.language_state.locked_profile,
        evidenced_profiles: session
            .evidenced_native_profiles
            .iter()
            .copied()
            .collect(),
    });
    if calibration_key.as_ref().is_some_and(|key| {
        session
            .split_calibrations
            .get(key)
            .is_some_and(|calibration| calibration.ineffective)
    }) {
        return false;
    }
    if let (Some(root_winner), Some(key)) = (root_winner, calibration_key) {
        let calibration = session
            .split_calibrations
            .get(&key)
            .copied()
            .unwrap_or_default();
        session.active_split_probe = Some(TableSplitProbe {
            calibration_key: (calibration.samples < SPLIT_CALIBRATION_SAMPLES).then_some(key),
            root_winner,
            descendant_start: session.jobs.len(),
            baseline_grammar_milli: session.confidence_milli[root_winner],
            baseline_mean_confidence_milli: session.mean_word_confidence_milli[root_winner],
        });
    }
    for index in session.active_attempts.iter().copied() {
        session.superseded[index] = true;
    }
    for child in children.into_iter().rev() {
        session.pending_contexts.push_front(child);
    }
    true
}

fn recursive_chunk_size(depth: u32, unit_count: usize, missing_native: bool) -> Option<usize> {
    if unit_count <= 1 {
        return None;
    }
    if depth == 0 {
        return Some(unit_count.min(64));
    }
    if unit_count > 16 {
        return Some(16);
    }
    if missing_native && unit_count > 4 {
        return Some(4);
    }
    if missing_native {
        return Some(1);
    }
    None
}

fn render_exact_rect_raster(
    pixels: &[u8],
    stride: usize,
    channels: usize,
    rect: Rect,
    segments: &[Rect],
) -> JobRaster {
    let width = (rect.right - rect.left) as usize;
    let height = (rect.bottom - rect.top) as usize;
    let output_stride = width.saturating_mul(3);
    let mut output = vec![255_u8; output_stride.saturating_mul(height)];
    for source_y in rect.top as usize..rect.bottom as usize {
        for source_x in rect.left as usize..rect.right as usize {
            let source = source_y * stride + source_x * channels;
            let target = (source_y - rect.top as usize) * output_stride
                + (source_x - rect.left as usize) * 3;
            match channels {
                1 => output[target..target + 3].fill(pixels[source]),
                3 => output[target..target + 3].copy_from_slice(&pixels[source..source + 3]),
                4 => {
                    let alpha = u16::from(pixels[source + 3]);
                    for channel in 0..3 {
                        let value = u16::from(pixels[source + channel]);
                        output[target + channel] =
                            ((value * alpha + 255 * (255 - alpha)) / 255) as u8;
                    }
                }
                _ => unreachable!("pixel format was validated"),
            }
        }
    }
    JobRaster {
        width: width as u32,
        height: height as u32,
        stride: output_stride as u32,
        pixels: output.into(),
        ocr_scale: 1,
        placements: segments
            .iter()
            .copied()
            .enumerate()
            .map(|(index, segment)| RasterPlacement {
                segment_indexes: vec![index],
                source_rect: segment,
                crop_rect: Rect {
                    left: segment.left.saturating_sub(rect.left),
                    top: segment.top.saturating_sub(rect.top),
                    right: segment.right.saturating_sub(rect.left),
                    bottom: segment.bottom.saturating_sub(rect.top),
                },
            })
            .collect(),
    }
}

#[allow(dead_code)] // Retained for explicitly bounded raster policies.
fn scaled_raster_coordinate(value: u32, input: u32, output: u32, ceil: bool) -> u32 {
    let numerator = u64::from(value).saturating_mul(u64::from(output));
    let denominator = u64::from(input);
    let scaled = if ceil {
        numerator.saturating_add(denominator.saturating_sub(1)) / denominator
    } else {
        numerator / denominator
    };
    u32::try_from(scaled).unwrap_or(output).min(output)
}

#[allow(dead_code)] // Canonical stage04 does not resize the saved raster.
fn bound_job_raster(mut raster: JobRaster) -> Option<JobRaster> {
    let (width, height) = compact::bounded_ocr_dimensions(
        raster.width as usize,
        raster.height as usize,
    )?;
    let width = u32::try_from(width).ok()?;
    let height = u32::try_from(height).ok()?;
    if width >= raster.width && height >= raster.height {
        return Some(raster);
    }
    let pixels = compact::resize_bilinear_rgb(
        &raster.pixels.decoded(),
        raster.width as usize,
        raster.height as usize,
        width as usize,
        height as usize,
    )?;
    for placement in &mut raster.placements {
        placement.crop_rect = Rect {
            left: scaled_raster_coordinate(
                placement.crop_rect.left,
                raster.width,
                width,
                false,
            ),
            top: scaled_raster_coordinate(
                placement.crop_rect.top,
                raster.height,
                height,
                false,
            ),
            right: scaled_raster_coordinate(
                placement.crop_rect.right,
                raster.width,
                width,
                true,
            ),
            bottom: scaled_raster_coordinate(
                placement.crop_rect.bottom,
                raster.height,
                height,
                true,
            ),
        };
    }
    raster.width = width;
    raster.height = height;
    raster.stride = width.checked_mul(3)?;
    raster.pixels = pixels.into();
    Some(raster)
}

#[derive(Default)]
struct Registry {
    next_handle: u32,
    sessions: BTreeMap<u32, Session>,
}

fn registry() -> &'static Mutex<Registry> {
    static REGISTRY: OnceLock<Mutex<Registry>> = OnceLock::new();
    REGISTRY.get_or_init(|| Mutex::new(Registry::default()))
}

fn channels(format: u32) -> Option<usize> {
    match format {
        1 => Some(1),
        3 => Some(3),
        4 => Some(4),
        _ => None,
    }
}

fn verified_route_jobs(
    pixels: &[u8],
    width: usize,
    height: usize,
    stride: usize,
    channels: usize,
) -> Option<(
    Vec<OcrJob>,
    Vec<JobRaster>,
    Vec<objects::DocumentObject>,
    Vec<blocks::RecognitionBlock>,
    topology::PhysicalTopology,
    BTreeMap<u32, Vec<MatrixCell>>,
)> {
    let analysis = geometry::analyze_geometry(pixels, width, height, stride, channels)?;
    let physical_topology = topology::build_physical_topology(&analysis)?;
    let reconstruction = objects::reconstruct_objects_with_topology(&analysis, &physical_topology)?;
    let plan = blocks::plan_blocks_with_topology(&analysis, &reconstruction, &physical_topology)?;
    if plan.blocks.len() > MAX_JOBS {
        return None;
    }
    let object_kind = |index: usize| match reconstruction.objects.get(index)?.kind {
        objects::ObjectKind::Paragraph => Some(OBJECT_PARAGRAPH),
        objects::ObjectKind::List => Some(OBJECT_LIST),
        objects::ObjectKind::Table => Some(OBJECT_TABLE),
        objects::ObjectKind::Unknown => Some(OBJECT_UNKNOWN),
        objects::ObjectKind::Flow => Some(OBJECT_FLOW),
    };
    let mut jobs = Vec::with_capacity(plan.blocks.len());
    let mut rasters = Vec::with_capacity(plan.blocks.len());
    compact::visit_compacted_blocks(&analysis, &plan, &reconstruction.objects, |index, compacted| {
        let block = &plan.blocks[index];
        let segments: Vec<Rect> = block
            .segment_indexes
            .iter()
            .map(|index| {
                let bbox = analysis.materialized_segments.segments[*index].bbox;
                Some(Rect {
                    left: u32::try_from(bbox[0]).ok()?,
                    top: u32::try_from(bbox[1]).ok()?,
                    right: u32::try_from(bbox[2]).ok()?,
                    bottom: u32::try_from(bbox[3]).ok()?,
                })
            })
            .collect::<Option<_>>()?;
        if block.logical_segment_spans.len() != block.segment_indexes.len() {
            return None;
        }
        let segment_cells: Vec<SegmentCell> = block
            .logical_segment_spans
            .iter()
            .map(|span| {
                Some(SegmentCell {
                    row: u32::try_from(span[0]).ok()?,
                    row_span: u32::try_from(span[1].saturating_sub(span[0])).ok()?,
                    column: u32::try_from(span[2]).ok()?,
                    column_span: u32::try_from(span[3].saturating_sub(span[2])).ok()?,
                })
            })
            .collect::<Option<_>>()?;
        let row = segment_cells.iter().map(|cell| cell.row).min()?;
        let column = segment_cells.iter().map(|cell| cell.column).min()?;
        let row_stop = segment_cells
            .iter()
            .map(|cell| cell.row.saturating_add(cell.row_span))
            .max()?;
        let column_stop = segment_cells
            .iter()
            .map(|cell| cell.column.saturating_add(cell.column_span))
            .max()?;
        let source_bbox = block.bbox;
        let rect = Rect {
            left: u32::try_from(source_bbox[0]).ok()?,
            top: u32::try_from(source_bbox[1]).ok()?,
            right: u32::try_from(source_bbox[2]).ok()?,
            bottom: u32::try_from(source_bbox[3]).ok()?,
        };
        let mut placements = Vec::<RasterPlacement>::new();
        for placement in &compacted.placements {
            let crop_rect = Rect {
                left: u32::try_from(placement.crop_bbox[0]).ok()?,
                top: u32::try_from(placement.crop_bbox[1]).ok()?,
                right: u32::try_from(placement.crop_bbox[2]).ok()?,
                bottom: u32::try_from(placement.crop_bbox[3]).ok()?,
            };
            let source_rect = Rect {
                left: u32::try_from(placement.source_bbox[0]).ok()?,
                top: u32::try_from(placement.source_bbox[1]).ok()?,
                right: u32::try_from(placement.source_bbox[2]).ok()?,
                bottom: u32::try_from(placement.source_bbox[3]).ok()?,
            };
            let segment_indexes = placement
                .segment_indexes
                .iter()
                .map(|segment_index| {
                    block
                        .segment_indexes
                        .iter()
                        .position(|value| value == segment_index)
                })
                .collect::<Option<Vec<_>>>()?;
            placements.push(RasterPlacement {
                segment_indexes,
                source_rect,
                crop_rect,
            });
        }
        let object_id = u32::try_from(block.scope_index).ok()?;
        let object_kind = object_kind(block.scope_index)?;
        jobs.push(OcrJob {
            rect,
            segments,
            source_segment_indexes: block.segment_indexes.clone(),
            segment_cells,
            compact_segments: true,
            object_id,
            row,
            column,
            row_span: row_stop.saturating_sub(row),
            column_span: column_stop.saturating_sub(column),
            recognition_mode: 0,
            object_kind,
            depth: 0,
            language_profile: LanguageProfileId::RusEng,
            transform: OcrTransform::Raw,
            logical_row_count: block.logical_scope_shape.map_or(row_stop, |shape| {
                u32::try_from(shape[0]).unwrap_or(u32::MAX)
            }),
            logical_column_count: block.logical_scope_shape.map_or(column_stop, |shape| {
                u32::try_from(shape[1]).unwrap_or(u32::MAX)
            }),
            ocr_required: !(matches!(object_kind, OBJECT_PARAGRAPH | OBJECT_LIST)
                && (rect.right.saturating_sub(rect.left) <= 1
                    || rect.bottom.saturating_sub(rect.top) <= 1)),
            words_in_source_space: false,
        imported_composite: false,
        });
        let raster = if object_kind != OBJECT_TABLE {
            render_exact_rect_raster(
                &analysis.foreground.pixels,
                analysis.foreground.width.checked_mul(3)?,
                3,
                rect,
                &jobs.last()?.segments,
            )
        } else {
            JobRaster {
                width: u32::try_from(compacted.width).ok()?,
                height: u32::try_from(compacted.height).ok()?,
                stride: u32::try_from(compacted.width.checked_mul(3)?).ok()?,
                pixels: compacted.pixels.into(),
                ocr_scale: 1,
                placements,
            }
        };
        rasters.push(raster);
        Some(())
    })?;
    let mut matrix_cells = BTreeMap::new();
    for (object_id, object) in reconstruction.objects.iter().enumerate() {
        if object.kind != objects::ObjectKind::Table { continue; }
        let Some(matrix) = &object.local_matrix else { continue; };
        let mut cells = Vec::new();
        for (row, value) in matrix.rows.iter().enumerate() {
            for (column, cell) in value.cells.iter().enumerate() {
                // Python recovery reads matrix_x/matrix_y as object-local OCR
                // coordinates. Preserve that boundary even when crop padding
                // makes the logical matrix origin differ from the crop origin.
                let b = cell.bbox;
                let x = |value: usize| value.checked_sub(object.matrix_bbox[0])?.checked_add(object.bbox[0]);
                let y = |value: usize| value.checked_sub(object.matrix_bbox[1])?.checked_add(object.bbox[1]);
                cells.push(MatrixCell {
                    cell: SegmentCell { row: u32::try_from(row).ok()?, column: u32::try_from(column).ok()?, row_span: 1, column_span: 1 },
                    rect: Rect { left: u32::try_from(x(b[0])?).ok()?, top: u32::try_from(y(b[1])?).ok()?, right: u32::try_from(x(b[2])?).ok()?, bottom: u32::try_from(y(b[3])?).ok()? },
                    source_segment_indexes: cell.segment_indexes.clone(),
                });
            }
        }
        matrix_cells.insert(u32::try_from(object_id).ok()?, cells);
    }
    let reconstructed_objects = reconstruction.objects;
    let planned_blocks = plan.blocks;
    Some((
        jobs,
        rasters,
        reconstructed_objects,
        planned_blocks,
        physical_topology,
        matrix_cells,
    ))
}

fn recognition_mode_for_rect(rect: Rect) -> u32 {
    let width = rect.right - rect.left;
    let height = rect.bottom - rect.top;
    if width >= 2_200 && height >= 1_500 && u64::from(height) * 10 <= u64::from(width) * 9 {
        2 // sparse text
    } else if width >= 1_600 && height >= 1_000 {
        1 // document
    } else {
        0 // ordinary text region
    }
}

fn intersection_area(left: Rect, right: Rect) -> u64 {
    let width = left
        .right
        .min(right.right)
        .saturating_sub(left.left.max(right.left));
    let height = left
        .bottom
        .min(right.bottom)
        .saturating_sub(left.top.max(right.top));
    u64::from(width) * u64::from(height)
}

fn project_crop_rect_to_source(rect: Rect, placement: &RasterPlacement) -> Rect {
    let crop_width = placement.crop_rect.right - placement.crop_rect.left;
    let crop_height = placement.crop_rect.bottom - placement.crop_rect.top;
    let source_width = placement.source_rect.right - placement.source_rect.left;
    let source_height = placement.source_rect.bottom - placement.source_rect.top;
    let project_x = |value: u32| {
        let local = value
            .clamp(placement.crop_rect.left, placement.crop_rect.right)
            .saturating_sub(placement.crop_rect.left);
        placement.source_rect.left.saturating_add(
            (f64::from(local) * f64::from(source_width) / f64::from(crop_width.max(1)))
                .round_ties_even() as u32,
        )
    };
    let project_y = |value: u32| {
        let local = value
            .clamp(placement.crop_rect.top, placement.crop_rect.bottom)
            .saturating_sub(placement.crop_rect.top);
        placement.source_rect.top.saturating_add(
            (f64::from(local) * f64::from(source_height) / f64::from(crop_height.max(1)))
                .round_ties_even() as u32,
        )
    };
    let left = project_x(rect.left).min(placement.source_rect.right.saturating_sub(1));
    let top = project_y(rect.top).min(placement.source_rect.bottom.saturating_sub(1));
    Rect {
        left,
        top,
        right: project_x(rect.right).clamp(left.saturating_add(1), placement.source_rect.right),
        bottom: project_y(rect.bottom).clamp(top.saturating_add(1), placement.source_rect.bottom),
    }
}

fn cell_text(candidates: &[(String, u32)]) -> String {
    let mut grouped: BTreeMap<String, (usize, u64)> = BTreeMap::new();
    for (text, confidence) in candidates {
        let normalized = text
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ");
        if normalized.is_empty() {
            continue;
        }
        let entry = grouped.entry(normalized).or_default();
        entry.0 += 1;
        entry.1 += u64::from(*confidence);
    }
    grouped
        .into_iter()
        .max_by_key(|(text, (count, confidence))| (*count, *confidence, text.len()))
        .map(|(text, _score)| text)
        .unwrap_or_default()
}

fn clean_table_cell(value: &str) -> String {
    value
        .split_whitespace()
        .filter(|token| !matches!(*token, "|" | "¦" | "│"))
        .collect::<Vec<_>>()
        .join(" ")
        .trim_matches([' ', '|', ',', '.', ':', ';'])
        .to_owned()
}

fn cell_text_by_job(candidates: &[(String, u32, usize)]) -> String {
    let mut grouped = BTreeMap::<String, (BTreeSet<usize>, usize, u64)>::new();
    for (text, confidence, job_index) in candidates {
        let normalized = clean_table_cell(text);
        if normalized.is_empty() {
            continue;
        }
        let entry = grouped.entry(normalized).or_default();
        entry.0.insert(*job_index);
        entry.1 += 1;
        entry.2 += u64::from(*confidence);
    }
    grouped
        .into_iter()
        .max_by(|(first_text, first), (second_text, second)| {
            first
                .0
                .len()
                .cmp(&second.0.len())
                .then_with(|| first.1.cmp(&second.1))
                .then_with(|| {
                    (first.2 * second.1 as u64).cmp(&(second.2 * first.1 as u64))
                })
                .then_with(|| first_text.chars().count().cmp(&second_text.chars().count()))
                .then_with(|| first_text.cmp(second_text))
        })
        .map(|(text, _score)| text)
        .unwrap_or_default()
}

fn word_center_in_source(job: &OcrJob, raster: &JobRaster, word: &OcrWordEvidence) -> (u64, u64) {
    if job.words_in_source_space {
        return (u64::from(word.rect.left) + u64::from(word.rect.right),
            u64::from(word.rect.top) + u64::from(word.rect.bottom));
    }
    // First undo the adapter's integer enlargement exactly: floor left/top,
    // ceil right/bottom. Python then maps this canonical crop rectangle through
    // the chosen compaction placement into the original block coordinates.
    let scale = raster.ocr_scale.max(1);
    let rect = Rect { left: word.rect.left / scale, top: word.rect.top / scale,
        right: word.rect.right.div_ceil(scale), bottom: word.rect.bottom.div_ceil(scale) };
    let crop = |p: &RasterPlacement| Rect { left: p.crop_rect.left / scale,
        top: p.crop_rect.top / scale, right: p.crop_rect.right / scale,
        bottom: p.crop_rect.bottom / scale };
    let placement = raster.placements.iter().enumerate().max_by_key(|(i, p)| {
        let c = crop(p);
        (intersection_area(rect, c), std::cmp::Reverse(
            (u64::from(rect.left) + u64::from(rect.right)).abs_diff(u64::from(c.left) + u64::from(c.right))), std::cmp::Reverse(*i))
    }).map(|(_, p)| p);
    let mapped = if let Some(p) = placement {
        let c = crop(p);
        // Preserve Python's ratio-before-multiplication order as well as
        // ties-to-even rounding; reassociation can change half-pixel results.
        let scale_x = f64::from(p.source_rect.right - p.source_rect.left)
            / f64::from((c.right - c.left).max(1));
        let scale_y = f64::from(p.source_rect.bottom - p.source_rect.top)
            / f64::from((c.bottom - c.top).max(1));
        let x = |value: u32| i64::from(p.source_rect.left) +
            (((i64::from(value) - i64::from(c.left)) as f64) * scale_x).round_ties_even() as i64;
        let y = |value: u32| i64::from(p.source_rect.top) +
            (((i64::from(value) - i64::from(c.top)) as f64) * scale_y).round_ties_even() as i64;
        let left = x(rect.left).clamp(i64::from(job.rect.left), i64::from(job.rect.right - 1)) as u32;
        let top = y(rect.top).clamp(i64::from(job.rect.top), i64::from(job.rect.bottom - 1)) as u32;
        Rect { left, top,
            right: x(rect.right).clamp(i64::from(left + 1), i64::from(job.rect.right)) as u32,
            bottom: y(rect.bottom).clamp(i64::from(top + 1), i64::from(job.rect.bottom)) as u32 }
    } else {
        Rect { left: job.rect.left + rect.left, top: job.rect.top + rect.top,
            right: job.rect.left + rect.right, bottom: job.rect.top + rect.bottom }
    };
    (u64::from(mapped.left) + u64::from(mapped.right), u64::from(mapped.top) + u64::from(mapped.bottom))
}

// Exact line clustering used by Python _reading_order_words. Because incoming
// centers are nondecreasing, the closest prior center is a line's maximum.
// Keeping that representative avoids rescanning every word already in a line.
fn reading_order_indices(rects: &[Rect]) -> Vec<usize> {
    let mut order = (0..rects.len()).collect::<Vec<_>>();
    let center = |index: usize| u64::from(rects[index].top) + u64::from(rects[index].bottom);
    order.sort_by_key(|&index| (center(index), rects[index].left, rects[index].right, index));
    let mut lines = Vec::<(usize, Vec<usize>)>::new();
    for index in order {
        let rect = rects[index];
        let mut best: Option<(f64, u64, usize)> = None;
        for (line_index, (representative, _)) in lines.iter().enumerate() {
            let previous = rects[*representative];
            let overlap = previous.bottom.min(rect.bottom).saturating_sub(previous.top.max(rect.top));
            let height = (previous.bottom - previous.top).min(rect.bottom - rect.top);
            let fraction = f64::from(overlap) / f64::from(height);
            let distance = center(index).abs_diff(center(*representative));
            if fraction >= 0.25 && best.is_none_or(|candidate| {
                fraction > candidate.0 || (fraction == candidate.0 &&
                    (distance < candidate.1 || (distance == candidate.1 && line_index > candidate.2)))
            }) { best = Some((fraction, distance, line_index)); }
        }
        if let Some((_, _, line_index)) = best {
            let (representative, members) = &mut lines[line_index];
            let previous = rects[*representative];
            if center(index) > center(*representative) ||
                (center(index) == center(*representative) && (rect.top, rect.left) < (previous.top, previous.left))
            { *representative = index; }
            members.push(index);
        } else { lines.push((index, vec![index])); }
    }
    let key = |line: &(usize, Vec<usize>)| {
        (line.1.iter().map(|&index| center(index)).sum::<u64>() as f64 / line.1.len() as f64,
         line.1.iter().map(|&index| rects[index].top).min().unwrap_or(0),
         line.1.iter().map(|&index| rects[index].left).min().unwrap_or(0))
    };
    lines.sort_by(|first, second| {
        let a = key(first); let b = key(second);
        a.0.total_cmp(&b.0).then_with(|| a.1.cmp(&b.1)).then_with(|| a.2.cmp(&b.2))
    });
    let mut result = Vec::new();
    for (_, mut line) in lines {
        line.sort_by_key(|&index| { let rect = rects[index]; (rect.left, rect.top, rect.right, rect.bottom, index) });
        result.extend(line);
    }
    result
}


fn reading_order_text(words: &[OcrWordEvidence]) -> String {
    let rects: Vec<_> = words.iter().map(|word| word.rect).collect();
    reading_order_indices(&rects).into_iter().map(|index| words[index].text.as_str()).collect::<Vec<_>>().join(" ")
}

fn sort_in_reading_order<T>(values: &mut Vec<T>, rect: impl Fn(&T) -> Rect) {
    // Pair-dependent overlap comparators form cycles and can panic inside
    // Rust's sort (or trap in WASM). Cluster first, then use only total keys.
    let rects: Vec<_> = values.iter().map(rect).collect();
    let order = reading_order_indices(&rects);
    let mut original: Vec<_> = std::mem::take(values).into_iter().map(Some).collect();
    values.extend(order.into_iter().map(|index| original[index].take().unwrap()));
}

fn materialize_matrix_segments(
    session: &Session,
    object_id: u32,
    indexes: &[usize],
    cells: &[MatrixCell],
) -> Vec<RecognizedSegment> {
    let mut candidates = vec![Vec::new(); cells.len()];
    for &index in indexes {
        let job = &session.jobs[index];
        let mut grouped = BTreeMap::<usize, Vec<&OcrWordEvidence>>::new();
        for word in &session.words[index] {
            let (x, y) = word_center_in_source(job, &session.job_rasters[index], word);
            // Preserve Python matrix traversal order and half-open cell bounds.
            if let Some(cell_index) = cells.iter().position(|cell| {
                u64::from(cell.rect.left) * 2 <= x && x < u64::from(cell.rect.right) * 2
                    && u64::from(cell.rect.top) * 2 <= y && y < u64::from(cell.rect.bottom) * 2
            }) {
                grouped.entry(cell_index).or_default().push(word);
            }
        }
        for (cell_index, words) in grouped {
            let text = words.iter().map(|word| word.text.as_str()).collect::<Vec<_>>().join(" ");
            let confidence = words.iter().map(|word| u64::from(word.ranking_confidence_units))
                .sum::<u64>() / words.len() as u64;
            candidates[cell_index].push((text, confidence as u32, index));
        }
    }
    cells.iter().zip(candidates).filter_map(|(cell, candidates)| {
        let text = cell_text_by_job(&candidates);
        // Python preserves source-backed empty cells and nonempty inferred cells.
        if text.is_empty() && cell.source_segment_indexes.is_empty() { return None; }
        let mut sources = cell.source_segment_indexes.clone();
        sources.sort_unstable(); sources.dedup();
        Some(RecognizedSegment {
            object_id, object_kind: OBJECT_TABLE, cell: cell.cell,
            source_segment_indexes: sources, text,
            evidence_job_indexes: candidates.iter().map(|candidate| candidate.2).collect(),
        })
    }).collect()
}

fn materialize_recognized_segments(session: &Session) -> Vec<RecognizedSegment> {
    let mut by_object = BTreeMap::<u32, Vec<usize>>::new();
    for index in 0..session.jobs.len() {
        if session.selected[index]
            && !session.superseded[index]
            && session.text[index].is_some()
        {
            by_object
                .entry(session.jobs[index].object_id)
                .or_default()
                .push(index);
        }
    }
    // Source object classification is the find-object boundary's output. The
    // Python get-segment contract renders lists as paragraph text; it does not
    // reinterpret aligned OCR words as a new table or merge adjacent objects.
    let mut output = Vec::new();
    for (object_id, indexes) in by_object {
        let object_kind = session.jobs[indexes[0]].object_kind;
        if object_kind != OBJECT_TABLE {
            let mut source_segment_indexes = indexes
                .iter()
                .flat_map(|index| session.jobs[*index].source_segment_indexes.iter().copied())
                .collect::<Vec<_>>();
            source_segment_indexes.sort_unstable();
            source_segment_indexes.dedup();
            let mut texts = Vec::new();
            for text in indexes
                .iter()
                .filter_map(|index| session.text[*index].as_deref().map(str::trim))
                .filter(|text| !text.is_empty())
            {
                if !texts.contains(&text) {
                    texts.push(text);
                }
            }
            let text = if indexes.len() == 1 && session.reading_order_objects.contains(&object_id)
                && !session.words[indexes[0]].is_empty() {
                reading_order_text(&session.words[indexes[0]])
            } else { texts.join("\n") };
            if text.trim().is_empty() { continue; }
            output.push(RecognizedSegment {
                object_id,
                object_kind: OBJECT_PARAGRAPH,
                source_segment_indexes,
                cell: SegmentCell {
                    row: 0,
                    column: 0,
                    row_span: 1,
                    column_span: 1,
                },
                text,
                evidence_job_indexes: indexes,
            });
            continue;
        }

        if let Some(cells) = session.matrix_cells.get(&object_id) {
            output.extend(materialize_matrix_segments(session, object_id, &indexes, cells));
            continue;
        }
        type SegmentKey = (u32, u32, u32, u32);
        let mut evidence = BTreeMap::<SegmentKey, (BTreeSet<usize>, Vec<usize>)>::new();
        let mut candidates = BTreeMap::<SegmentKey, Vec<(String, u32, usize)>>::new();
        for index in &indexes {
            let job = &session.jobs[*index];
            for (segment_index, cell) in job.segment_cells.iter().copied().enumerate() {
                let Some(source_index) = job.source_segment_indexes.get(segment_index).copied()
                else {
                    continue;
                };
                let entry = evidence
                    .entry((cell.row, cell.column, cell.row_span, cell.column_span))
                    .or_default();
                entry.0.insert(source_index);
                entry.1.push(*index);
            }
            let mut words_by_segment = BTreeMap::<SegmentKey, Vec<&OcrWordEvidence>>::new();
            for word in &session.words[*index] {
                let (center_x_twice, center_y_twice) = word_center_in_source(job, &session.job_rasters[*index], word);
                let Some(segment_index) = job.segments.iter().position(|source| {
                    u64::from(source.left) * 2 <= center_x_twice
                        && center_x_twice < u64::from(source.right) * 2
                        && u64::from(source.top) * 2 <= center_y_twice
                        && center_y_twice < u64::from(source.bottom) * 2
                })
                else {
                    continue;
                };
                let Some(cell) = job.segment_cells.get(segment_index).copied() else {
                    continue;
                };
                let Some(_source_index) = job.source_segment_indexes.get(segment_index).copied()
                else {
                    continue;
                };
                words_by_segment
                    .entry((cell.row, cell.column, cell.row_span, cell.column_span))
                    .or_default()
                    .push(word);
            }
            for (key, words) in words_by_segment {
                let text = words
                    .iter()
                    .map(|word| word.text.as_str())
                    .collect::<Vec<_>>()
                    .join(" ");
                let confidence = words
                    .iter()
                    .map(|word| u64::from(word.ranking_confidence_units))
                    .sum::<u64>()
                    .checked_div(words.len() as u64)
                    .unwrap_or(0) as u32;
                candidates
                    .entry(key)
                    .or_default()
                    .push((text, confidence, *index));
            }
        }
        for (key, (source_segment_indexes, mut job_indexes)) in evidence {
            job_indexes.sort_unstable();
            job_indexes.dedup();
            output.push(RecognizedSegment {
                object_id,
                object_kind,
                source_segment_indexes: source_segment_indexes.into_iter().collect(),
                cell: SegmentCell {
                    row: key.0,
                    column: key.1,
                    row_span: key.2,
                    column_span: key.3,
                },
                text: cell_text_by_job(candidates.get(&key).map_or(&[], Vec::as_slice)),
                evidence_job_indexes: job_indexes,
            });
        }
    }
    output.sort_by_key(|segment| {
        (
            segment.object_id,
            segment.cell.row,
            segment.cell.column,
            segment
                .source_segment_indexes
                .first()
                .copied()
                .unwrap_or(usize::MAX),
        )
    });
    output
}

fn inferred_table_column_anchors(
    session: &Session,
    indexes: &[usize],
    declared_column_count: usize,
) -> Option<Vec<u32>> {
    let source_rectangles = indexes
        .iter()
        .flat_map(|index| session.jobs[*index].segments.iter().copied())
        .map(|rect| (rect.left, rect.top, rect.right, rect.bottom))
        .collect::<BTreeSet<_>>();
    let left = source_rectangles.iter().map(|rect| rect.0).min()?;
    let right = source_rectangles.iter().map(|rect| rect.2).max()?;
    let width = right.saturating_sub(left);
    if width < 2 {
        return None;
    }
    let mut centers = source_rectangles
        .iter()
        .filter(|rect| rect.2.saturating_sub(rect.0).saturating_mul(3) <= width)
        .map(|rect| rect.0.saturating_add(rect.2).div_ceil(2))
        .collect::<Vec<_>>();
    centers.sort_unstable();
    let tolerance = (width / 20).max(8);
    let mut clusters = Vec::<(u64, u32)>::new();
    for center in centers {
        if let Some((sum, count)) = clusters.last_mut()
            && center.abs_diff((*sum / u64::from(*count)) as u32) <= tolerance
        {
            *sum += u64::from(center);
            *count += 1;
        } else {
            clusters.push((u64::from(center), 1));
        }
    }
    let anchors = clusters
        .into_iter()
        .map(|(sum, count)| (sum / u64::from(count)) as u32)
        .collect::<Vec<_>>();
    (anchors.len() >= 2 && anchors.len().saturating_mul(2) <= declared_column_count)
        .then_some(anchors)
}

fn nearest_column_anchor(source: Rect, anchors: &[u32]) -> u32 {
    let center = source.left.saturating_add(source.right).div_ceil(2);
    anchors
        .iter()
        .enumerate()
        .min_by_key(|(index, anchor)| (center.abs_diff(**anchor), *index))
        .map_or(0, |(index, _anchor)| index as u32)
}

fn render_table_object(session: &Session, indexes: &[usize]) -> String {
    if indexes.iter().all(|index| session.words[*index].is_empty()) {
        return indexes
            .iter()
            .filter_map(|index| session.text[*index].as_deref().map(str::trim))
            .filter(|text| !text.is_empty())
            .collect::<Vec<_>>()
            .join("\n");
    }
    let first_row = 0;
    let row_count = indexes
        .iter()
        .map(|index| session.jobs[*index].logical_row_count)
        .max()
        .unwrap_or(0) as usize;
    let declared_column_count = indexes
        .iter()
        .map(|index| session.jobs[*index].logical_column_count)
        .max()
        .unwrap_or(0) as usize;
    let inferred_anchors = inferred_table_column_anchors(session, indexes, declared_column_count);
    let column_count = inferred_anchors
        .as_ref()
        .map_or(declared_column_count, Vec::len);
    if row_count == 0 || column_count == 0 {
        return String::new();
    }

    let mut segment_evidence: BTreeMap<(u32, u32, u32, u32, u32, u32), Vec<(String, u32)>> =
        BTreeMap::new();
    for index in indexes {
        let job = &session.jobs[*index];
        let raster = &session.job_rasters[*index];
        let words = &session.words[*index];
        let mut mapped: BTreeMap<(u32, usize), Vec<(&OcrWordEvidence, Rect)>> = BTreeMap::new();
        for word in words {
            let Some((placement, _area)) = raster
                .placements
                .iter()
                .filter_map(|placement| {
                    let area = intersection_area(word.rect, placement.crop_rect);
                    (area > 0).then_some((placement, area))
                })
                .max_by_key(|(placement, area)| {
                    (
                        *area,
                        usize::MAX - placement.segment_indexes.first().copied().unwrap_or(0),
                    )
                })
            else {
                continue;
            };
            let source_word = project_crop_rect_to_source(word.rect, placement);
            let Some((segment_index, _area)) = placement
                .segment_indexes
                .iter()
                .copied()
                .filter_map(|segment_index| {
                    let area = intersection_area(source_word, job.segments[segment_index]);
                    (area > 0).then_some((segment_index, area))
                })
                .max_by_key(|(segment_index, area)| (*area, usize::MAX - *segment_index))
            else {
                continue;
            };
            if let Some(cell) = job.segment_cells.get(segment_index) {
                mapped
                    .entry((cell.row, segment_index))
                    .or_default()
                    .push((word, source_word));
            }
        }
        for ((row, segment_index), mut segment_words) in mapped {
            sort_in_reading_order(&mut segment_words, |(_, rect)| *rect);
            let source = job.segments[segment_index];
            let mut column_words = BTreeMap::<u32, Vec<(&OcrWordEvidence, Rect)>>::new();
            if let Some(anchors) = inferred_anchors.as_ref() {
                let anchor_span = anchors
                    .last()
                    .copied()
                    .unwrap_or(0)
                    .saturating_sub(anchors.first().copied().unwrap_or(0));
                let broad =
                    source.right.saturating_sub(source.left).saturating_mul(2) > anchor_span;
                if broad && segment_words.len() >= anchors.len() {
                    let mut gaps = segment_words
                        .windows(2)
                        .enumerate()
                        .map(|(index, pair)| {
                            (
                                pair[1].0.rect.left.saturating_sub(pair[0].0.rect.right),
                                index,
                            )
                        })
                        .collect::<Vec<_>>();
                    gaps.sort_by_key(|(gap, index)| (std::cmp::Reverse(*gap), *index));
                    let cuts = gaps
                        .into_iter()
                        .take(anchors.len().saturating_sub(1))
                        .map(|(_gap, index)| index)
                        .collect::<BTreeSet<_>>();
                    let mut column = 0_u32;
                    for (index, word) in segment_words.into_iter().enumerate() {
                        column_words.entry(column).or_default().push(word);
                        if cuts.contains(&index) {
                            column = column.saturating_add(1);
                        }
                    }
                } else {
                    let column = nearest_column_anchor(source, anchors);
                    column_words.insert(column, segment_words);
                }
            } else {
                let column = job.segment_cells[segment_index].column;
                column_words.insert(column, segment_words);
            }
            for (column, words) in column_words {
                let text = words
                    .iter()
                    .map(|(word, _source)| word.text.as_str())
                    .collect::<Vec<_>>()
                    .join(" ");
                let confidence = words
                    .iter()
                    .map(|(word, _source)| u64::from(word.confidence_milli))
                    .sum::<u64>()
                    .checked_div(words.len() as u64)
                    .unwrap_or(0) as u32;
                segment_evidence
                    .entry((
                        row,
                        column,
                        source.left,
                        source.top,
                        source.right,
                        source.bottom,
                    ))
                    .or_default()
                    .push((text, confidence));
            }
        }
        if words.is_empty() {
            let mut cells = job.segment_cells.clone();
            cells.sort_by_key(|cell| (cell.row, cell.column));
            cells.dedup();
            if cells.len() == 1 {
                if let Some(text) = session.text[*index].as_deref().map(str::trim)
                    && !text.is_empty()
                {
                    let source = job.segments[0];
                    let column = inferred_anchors
                        .as_ref()
                        .map_or(cells[0].column, |anchors| {
                            nearest_column_anchor(source, anchors)
                        });
                    segment_evidence
                        .entry((
                            cells[0].row,
                            column,
                            source.left,
                            source.top,
                            source.right,
                            source.bottom,
                        ))
                        .or_default()
                        .push((text.to_owned(), session.confidence_milli[*index]));
                }
            }
        }
    }

    let mut cell_segments: BTreeMap<(u32, u32), Vec<(Rect, String)>> = BTreeMap::new();
    for ((row, column, left, top, right, bottom), candidates) in segment_evidence {
        let text = cell_text(&candidates);
        if !text.is_empty() {
            cell_segments.entry((row, column)).or_default().push((
                Rect {
                    left,
                    top,
                    right,
                    bottom,
                },
                text,
            ));
        }
    }
    let mut rows = vec![vec![String::new(); column_count]; row_count];
    for ((row, column), mut segments) in cell_segments {
        let local_row = row.saturating_sub(first_row) as usize;
        if local_row < row_count && (column as usize) < column_count {
            sort_in_reading_order(&mut segments, |(rect, _)| *rect);
            rows[local_row][column as usize] = segments
                .into_iter()
                .map(|(_source, text)| text)
                .collect::<Vec<_>>()
                .join(" ");
        }
    }
    let render_row = |row: &[String]| {
        format!(
            "| {} |",
            row.iter()
                .map(|value| value.replace('\n', " ").replace('|', "\\|"))
                .collect::<Vec<_>>()
                .join(" | ")
        )
    };
    let mut lines = vec![render_row(&rows[0])];
    lines.push(render_row(&vec!["---".to_owned(); column_count]));
    lines.extend(rows.iter().skip(1).map(|row| render_row(row)));
    lines.join("\n")
}

fn finalize_recognized_layouts(session: &mut Session) {
    for segment in session.recognized_segments.iter().flatten() {
        let bounds = session.recognized_layouts
            .entry(segment.object_id)
            .or_insert((segment.object_kind, 0, 0));
        bounds.1 = bounds.1.max(segment.cell.row.saturating_add(segment.cell.row_span));
        bounds.2 = bounds.2.max(segment.cell.column.saturating_add(segment.cell.column_span));
    }
    // Empty edge cells are part of a table's logical scope even when no OCR
    // word produced a segment there. Recover that scope from the actual jobs.
    for job in &session.jobs {
        if job.object_kind == OBJECT_TABLE
            && let Some(bounds) = session.recognized_layouts.get_mut(&job.object_id)
            && bounds.0 == OBJECT_TABLE
        {
            bounds.1 = bounds.1.max(job.logical_row_count);
            bounds.2 = bounds.2.max(job.logical_column_count);
        }
    }
}

fn render_recognized_segments(
    segments: &[RecognizedSegment],
    object_layouts: &BTreeMap<u32, (u32, u32, u32)>,
) -> Vec<u8> {
    use crate::assembler::{
        AssemblerSegment, AssemblerSource, SegmentObjectLayout, StructuralObjectKind,
        assemble_segment_topology,
    };

    let object_kind = |kind| {
        if kind == OBJECT_TABLE {
            StructuralObjectKind::Table
        } else {
            StructuralObjectKind::Paragraph
        }
    };
    let mut object_bounds = object_layouts.clone();
    let assembler_segments = segments
        .iter()
        .enumerate()
        .map(|(index, segment)| {
            let bounds = object_bounds
                .entry(segment.object_id)
                .or_insert((segment.object_kind, 0, 0));
            bounds.1 = bounds
                .1
                .max(segment.cell.row.saturating_add(segment.cell.row_span));
            bounds.2 = bounds
                .2
                .max(segment.cell.column.saturating_add(segment.cell.column_span));
            AssemblerSegment {
                segment_id: format!("segment-{index:06}"),
                object_id: format!("object-{:06}", segment.object_id),
                object_kind: object_kind(segment.object_kind),
                row: segment.cell.row,
                column: segment.cell.column,
                row_span: segment.cell.row_span,
                column_span: segment.cell.column_span,
                text: segment.text.clone(),
            }
        })
        .collect::<Vec<_>>();
    let layouts = object_bounds
        .into_iter()
        .map(|(object_id, (kind, logical_row_count, logical_column_count))| {
            SegmentObjectLayout {
                object_id: format!("object-{object_id:06}"),
                object_kind: object_kind(kind),
                logical_row_count,
                logical_column_count,
            }
        })
        .collect::<Vec<_>>();
    let markdown = assemble_segment_topology(
        AssemblerSource::RasterGeometry,
        &layouts,
        &assembler_segments,
    )
    .map(|artifact| artifact.markdown)
    .unwrap_or_default();
    format!("# result\n\n{markdown}").into_bytes()
}

fn render_session(session: &Session) -> Vec<u8> {
    if let Some(segments) = session.recognized_segments.as_deref() {
        return render_recognized_segments(segments, &session.recognized_layouts);
    }
    let terminal_jobs: Vec<usize> = (0..session.jobs.len())
        .filter(|index| !session.superseded[*index] && session.text[*index].is_some())
        .collect();
    let mut by_object: BTreeMap<u32, Vec<usize>> = BTreeMap::new();
    for index in terminal_jobs {
        by_object
            .entry(session.jobs[index].object_id)
            .or_default()
            .push(index);
    }
    let mut objects: Vec<(usize, u32, Vec<usize>)> = by_object
        .into_iter()
        .map(|(object_id, mut indexes)| {
            indexes.sort_by_key(|index| {
                let job = &session.jobs[*index];
                (job.row, job.column, job.depth, *index)
            });
            let reading_index = session
                .objects
                .get(object_id as usize)
                .map_or(object_id as usize, |object| object.reading_index);
            (reading_index, object_id, indexes)
        })
        .collect();
    objects.sort_by_key(|(reading_index, object_id, _indexes)| (*reading_index, *object_id));

    let mut rendered_objects = Vec::new();
    for (_reading_index, object_id, indexes) in objects {
        let object_kind = session.jobs[indexes[0]].object_kind;
        let rendered = if object_kind == OBJECT_TABLE {
            let evidence_indexes = (0..session.jobs.len())
                .filter(|index| {
                    session.selected[*index]
                        && session.jobs[*index].object_id == object_id
                        && session.text[*index].is_some()
                })
                .collect::<Vec<_>>();
            render_table_object(session, &evidence_indexes)
        } else {
            indexes
                .iter()
                .filter_map(|index| session.text[*index].as_deref().map(str::trim))
                .filter(|text| !text.is_empty())
                .collect::<Vec<_>>()
                .join("\n")
        };
        if !rendered.is_empty() {
            rendered_objects.push(rendered);
        }
    }
    rendered_objects.join("\n\n").into_bytes()
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_alloc(length: u32) -> *mut u8 {
    if length == 0 {
        return std::ptr::null_mut();
    }
    let mut bytes = Vec::<u8>::with_capacity(length as usize);
    let pointer = bytes.as_mut_ptr();
    std::mem::forget(bytes);
    pointer
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_dealloc(pointer: *mut u8, capacity: u32) {
    if pointer.is_null() || capacity == 0 {
        return;
    }
    // SAFETY: callers only return pointers and capacities obtained from ittm_alloc.
    unsafe { drop(Vec::from_raw_parts(pointer, 0, capacity as usize)) };
}

unsafe fn planned_session_from_raw(
    pixels: *const u8,
    pixel_length: u32,
    width: u32,
    height: u32,
    stride: u32,
    format: u32,
) -> Option<Session> {
    let Some(channel_count) = channels(format) else {
        return None;
    };
    let minimum_stride = match (width as usize).checked_mul(channel_count) {
        Some(value) => value,
        None => return None,
    };
    let expected_length = match (stride as usize).checked_mul(height as usize) {
        Some(value) => value,
        None => return None,
    };
    if pixels.is_null()
        || width == 0
        || height == 0
        || (stride as usize) < minimum_stride
        || pixel_length as usize != expected_length
    {
        return None;
    }
    // SAFETY: the validated byte range is owned by the caller for this call.
    let input = unsafe { slice::from_raw_parts(pixels, expected_length) };
    let Some((jobs, job_rasters, objects, blocks, topology, matrix_cells)) = verified_route_jobs(
        input,
        width as usize,
        height as usize,
        stride as usize,
        channel_count,
    ) else {
        return None;
    };
    let block_rasters = job_rasters.clone();
    let mut pending_contexts = VecDeque::new();
    for (job, raster) in jobs.into_iter().zip(job_rasters) {
        if job.ocr_required {
            pending_contexts.push_back(PendingContext { job, raster });
        }
    }
    Some(Session {
        objects,
        blocks,
        block_rasters,
        jobs: Vec::new(),
        job_rasters: Vec::new(),
        text: Vec::new(),
        confidence_milli: Vec::new(),
        mean_word_confidence_milli: Vec::new(),
        words: Vec::new(),
        superseded: Vec::new(),
        selected: Vec::new(),
        pending_contexts,
        active_context: None,
        active_agenda: None,
        active_attempts: Vec::new(),
        language_state: LanguageSplayState::default(),
        evidenced_native_profiles: BTreeSet::new(),
        table_profile_discovery_complete: false,
        split_calibrations: BTreeMap::new(),
        active_split_probe: None,
        recognized_segments: None,
        recognized_layouts: BTreeMap::new(),
        matrix_cells,
        reading_order_objects: BTreeSet::new(),
        importing_ocr: false,
        topology,
        rendered: None,
        stage_mask: PLANNED_STAGE_MASK,
    })
}

fn empty_imported_plan_session() -> Session {
    Session {
        objects: Vec::new(),
        blocks: Vec::new(),
        block_rasters: Vec::new(),
        jobs: Vec::new(),
        job_rasters: Vec::new(),
        text: Vec::new(),
        confidence_milli: Vec::new(),
        mean_word_confidence_milli: Vec::new(),
        words: Vec::new(),
        superseded: Vec::new(),
        selected: Vec::new(),
        pending_contexts: VecDeque::new(),
        active_context: None,
        active_agenda: None,
        active_attempts: Vec::new(),
        language_state: LanguageSplayState::default(),
        evidenced_native_profiles: BTreeSet::new(),
        table_profile_discovery_complete: false,
        split_calibrations: BTreeMap::new(),
        active_split_probe: None,
        recognized_segments: None,
        recognized_layouts: BTreeMap::new(),
        matrix_cells: BTreeMap::new(),
        reading_order_objects: BTreeSet::new(),
        importing_ocr: false,
        topology: topology::PhysicalTopology { rows: Vec::new() },
        rendered: None,
        stage_mask: PLANNED_STAGE_MASK,
    }
}

fn register_session(session: Session) -> Option<u32> {
    let Ok(mut registry) = registry().lock() else {
        return None;
    };
    registry.next_handle = registry.next_handle.wrapping_add(1).max(1);
    let handle = registry.next_handle;
    registry.sessions.insert(handle, session);
    Some(handle)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_separated_plan_begin(
    pixels: *const u8,
    pixel_length: u32,
    width: u32,
    height: u32,
    stride: u32,
    format: u32,
) -> u32 {
    // SAFETY: `planned_session_from_raw` validates the caller-owned byte range.
    let Some(session) = (unsafe {
        planned_session_from_raw(pixels, pixel_length, width, height, stride, format)
    }) else {
        return 0;
    };
    register_session(session).unwrap_or(0)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_import_blocks_begin() -> u32 {
    register_session(empty_imported_plan_session()).unwrap_or(0)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_import_segments_begin() -> u32 {
    let mut session = empty_imported_plan_session();
    session.stage_mask = (1 << 7) - 1;
    session.recognized_segments = Some(Vec::new());
    register_session(session).unwrap_or(0)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_import_layout(
    handle: u32,
    object_id: u32,
    object_kind: u32,
    rows: u32,
    columns: u32,
) -> i32 {
    if object_kind > OBJECT_FLOW {
        return -1;
    }
    let Ok(mut registry) = registry().lock() else { return -2; };
    let Some(session) = registry.sessions.get_mut(&handle) else { return -2; };
    if session.recognized_segments.is_none() || session.rendered.is_some() {
        return -2;
    }
    if session.recognized_layouts.contains_key(&object_id) {
        return -3;
    }
    session.recognized_layouts.insert(object_id, (object_kind, rows, columns));
    0
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_layout_count(handle: u32) -> u32 {
    let Ok(registry) = registry().lock() else { return 0; };
    registry.sessions.get(&handle)
        .map_or(0, |session| session.recognized_layouts.len() as u32)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_layout_field(handle: u32, index: u32, field: u32) -> i32 {
    let Ok(registry) = registry().lock() else { return -1; };
    let Some(session) = registry.sessions.get(&handle) else { return -1; };
    let Some((&object_id, &(kind, rows, columns))) =
        session.recognized_layouts.iter().nth(index as usize)
    else { return -1; };
    match field {
        0 => object_id as i32,
        1 => kind as i32,
        2 => rows as i32,
        3 => columns as i32,
        _ => -1,
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_separated_import_segment(
    handle: u32,
    object_id: u32,
    object_kind: u32,
    row: u32,
    column: u32,
    row_span: u32,
    column_span: u32,
    source_indexes: *const u32,
    source_count: u32,
    text: *const u8,
    text_length: u32,
) -> i32 {
    if object_kind > OBJECT_FLOW
        || row_span == 0
        || column_span == 0
        || (source_indexes.is_null() && source_count != 0)
        || (text.is_null() && text_length != 0)
    {
        return -1;
    }
    let sources = if source_count == 0 {
        &[][..]
    } else {
        // SAFETY: the caller owns source_count values for this call.
        unsafe { slice::from_raw_parts(source_indexes, source_count as usize) }
    };
    let text_bytes = if text_length == 0 {
        &[][..]
    } else {
        // SAFETY: the caller owns text_length bytes for this call.
        unsafe { slice::from_raw_parts(text, text_length as usize) }
    };
    let Ok(text) = str::from_utf8(text_bytes) else {
        return -2;
    };
    let Ok(mut registry) = registry().lock() else {
        return -3;
    };
    let Some(segments) = registry
        .sessions
        .get_mut(&handle)
        .and_then(|session| session.recognized_segments.as_mut())
    else {
        return -3;
    };
    segments.push(RecognizedSegment {
        object_id,
        object_kind,
        source_segment_indexes: sources.iter().map(|value| *value as usize).collect(),
        cell: SegmentCell {
            row,
            column,
            row_span,
            column_span,
        },
        text: text.to_owned(),
        evidence_job_indexes: Vec::new(),
    });
    0
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_import_segments_finish(handle: u32) -> i32 {
    let Ok(mut registry) = registry().lock() else {
        return 0;
    };
    let Some(session) = registry.sessions.get_mut(&handle) else {
        return 0;
    };
    let Some(segments) = session.recognized_segments.as_mut() else {
        return 0;
    };
    if segments.is_empty() && session.recognized_layouts.is_empty() {
        return 0;
    }
    segments.sort_by_key(|segment| {
        (
            segment.object_id,
            segment.cell.row,
            segment.cell.column,
            segment.source_segment_indexes.first().copied().unwrap_or(usize::MAX),
        )
    });
    session.stage_mask = (1 << 7) - 1;
    finalize_recognized_layouts(session);
    1
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_separated_import_block(
    handle: u32, metadata: *const u32, metadata_length: u32,
    pixels: *const u8, pixel_length: u32, width: u32, height: u32, stride: u32,
) -> i32 {
    unsafe { import_block(handle, metadata, metadata_length, pixels, pixel_length, width, height, stride, false) }
}

// Stage 06 consumes block geometry and recognized words, never source pixels.
// This explicit entry point permits large frozen Python corpora in WASM without
// allocating gigabytes of repeated OCR contexts or inventing replacement images.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_separated_import_block_geometry(
    handle: u32, metadata: *const u32, metadata_length: u32,
    width: u32, height: u32, stride: u32,
) -> i32 {
    unsafe { import_block(handle, metadata, metadata_length, std::ptr::null(), 0, width, height, stride, true) }
}

unsafe fn import_block(
    handle: u32, metadata: *const u32, metadata_length: u32,
    pixels: *const u8, pixel_length: u32, width: u32, height: u32, stride: u32,
    geometry_only: bool,
) -> i32 {
    if metadata.is_null() || metadata_length == 0 || width == 0 || height == 0
        || stride < width.saturating_mul(3)
        || (!geometry_only && (pixels.is_null() || pixel_length != stride.saturating_mul(height)))
    { return -1; }
    // SAFETY: the caller owns the metadata range for this call.
    let metadata = unsafe { slice::from_raw_parts(metadata, metadata_length as usize) };
    let pixels = if geometry_only { &[][..] } else {
        // SAFETY: the pixel range length and non-null pointer were checked above.
        unsafe { slice::from_raw_parts(pixels, pixel_length as usize) }
    };
    let mut cursor = ImportCursor::new(metadata);
    let version = cursor.take();
    if !matches!(version, Some(1 | 2 | 3)) {
        return -2;
    }
    let Some(object_id) = cursor.take() else {
        return -3;
    };
    let Some(object_kind) = cursor.take() else {
        return -3;
    };
    if object_kind > OBJECT_FLOW {
        return -3;
    }
    let Some(object_rect) = cursor.rect() else {
        return -3;
    };
    let Some(block_rect) = cursor.rect() else {
        return -3;
    };
    let compact_segments = cursor.take() == Some(1);
    let matrix_present = cursor.take() == Some(1);
    let Some(matrix_rect) = cursor.rect() else {
        return -3;
    };
    let Some(logical_row_count) = cursor.take() else {
        return -3;
    };
    let Some(logical_column_count) = cursor.take() else {
        return -3;
    };
    let Some(core_segment_indexes) = cursor.indexes() else {
        return -3;
    };
    let Some(segment_count) = cursor.take().map(|value| value as usize) else {
        return -3;
    };
    let mut segment_indexes = Vec::with_capacity(segment_count);
    let mut segments = Vec::with_capacity(segment_count);
    let mut segment_cells = Vec::with_capacity(segment_count);
    let mut logical_segment_spans = Vec::with_capacity(segment_count);
    for _ in 0..segment_count {
        let Some(segment_index) = cursor.take() else {
            return -3;
        };
        let Some(source_rect) = cursor.rect() else {
            return -3;
        };
        let Some(row) = cursor.take() else {
            return -3;
        };
        let Some(column) = cursor.take() else {
            return -3;
        };
        let Some(row_span) = cursor.take() else {
            return -3;
        };
        let Some(column_span) = cursor.take() else {
            return -3;
        };
        if row_span == 0 || column_span == 0 {
            return -3;
        }
        segment_indexes.push(segment_index as usize);
        segments.push(source_rect);
        segment_cells.push(SegmentCell {
            row,
            column,
            row_span,
            column_span,
        });
        logical_segment_spans.push([
            row as usize,
            row.saturating_add(row_span) as usize,
            column as usize,
            column.saturating_add(column_span) as usize,
        ]);
    }
    let Some(context_segment_indexes) = cursor.indexes() else {
        return -3;
    };
    let Some(placement_count) = cursor.take().map(|value| value as usize) else {
        return -3;
    };
    let mut placements = Vec::with_capacity(placement_count);
    for _ in 0..placement_count {
        let Some(source_rect) = cursor.rect() else {
            return -3;
        };
        let Some(crop_rect) = cursor.rect() else {
            return -3;
        };
        let Some(placement_segments) = cursor.indexes() else {
            return -3;
        };
        placements.push(RasterPlacement {
            segment_indexes: placement_segments,
            source_rect,
            crop_rect,
        });
    }
    // ABI v2 appends the complete logical matrix; v1 remains unchanged.
    let mut matrix_cells = Vec::new();
    if matches!(version, Some(2 | 3)) {
        let Some(count) = cursor.take() else { return -3; };
        let mut seen = BTreeSet::new();
        for _ in 0..count {
            let (Some(row), Some(column), Some(row_span), Some(column_span)) =
                (cursor.take(), cursor.take(), cursor.take(), cursor.take())
            else { return -3; };
            let Some(rect) = cursor.rect() else { return -3; };
            let Some(source_segment_indexes) = cursor.indexes() else { return -3; };
            if row_span == 0 || column_span == 0 || rect.left >= rect.right || rect.top >= rect.bottom
                || row.checked_add(row_span).is_none_or(|end| end > logical_row_count)
                || column.checked_add(column_span).is_none_or(|end| end > logical_column_count)
                || !seen.insert((row, column, row_span, column_span))
            { return -3; }
            matrix_cells.push(MatrixCell {
                cell: SegmentCell { row, column, row_span, column_span }, rect,
                source_segment_indexes,
            });
        }
    }
    let reading_order = if version == Some(3) {
        match cursor.take() { Some(0) => false, Some(1) => true, _ => return -3 }
    } else { false };
    if !cursor.finished()
        || segment_indexes.is_empty()
        || block_rect.left >= block_rect.right
        || block_rect.top >= block_rect.bottom
        || object_rect.left >= object_rect.right
        || object_rect.top >= object_rect.bottom
    {
        return -3;
    }
    let object_kind_value = match object_kind {
        OBJECT_PARAGRAPH => objects::ObjectKind::Paragraph,
        OBJECT_LIST => objects::ObjectKind::List,
        OBJECT_TABLE => objects::ObjectKind::Table,
        OBJECT_FLOW => objects::ObjectKind::Flow,
        _ => objects::ObjectKind::Unknown,
    };
    let raster = JobRaster {
        width,
        height,
        stride,
        pixels: pixels.to_vec().into(),
        ocr_scale: 1,
        placements,
    };
    let matrix_window = matrix_present.then_some([
        matrix_rect.left as usize,
        matrix_rect.top as usize,
        matrix_rect.right as usize,
        matrix_rect.bottom as usize,
    ]);
    let job = OcrJob {
        rect: block_rect,
        segments,
        source_segment_indexes: segment_indexes.clone(),
        segment_cells,
        compact_segments,
        object_id,
        row: if matrix_present { matrix_rect.left } else { 0 },
        column: if matrix_present { matrix_rect.right } else { 0 },
        row_span: if matrix_present {
            matrix_rect.top.saturating_sub(matrix_rect.left)
        } else {
            1
        },
        column_span: if matrix_present {
            matrix_rect.bottom.saturating_sub(matrix_rect.right)
        } else {
            1
        },
        recognition_mode: 0,
        object_kind,
        depth: 0,
        language_profile: LanguageProfileId::RusEng,
        transform: OcrTransform::Raw,
        logical_row_count: logical_row_count.max(1),
        logical_column_count: logical_column_count.max(1),
        ocr_required: true,
        words_in_source_space: false,
        imported_composite: false,
    };

    let Ok(mut registry) = registry().lock() else {
        return -4;
    };
    let Some(session) = registry.sessions.get_mut(&handle) else {
        return -4;
    };
    if session.active_context.is_some() || !session.jobs.is_empty() {
        return -5;
    }
    if !matrix_cells.is_empty() {
        if let Some(previous) = session.matrix_cells.get(&object_id) {
            if previous != &matrix_cells { return -3; }
        } else {
            session.matrix_cells.insert(object_id, matrix_cells);
        }
    }
    if reading_order { session.reading_order_objects.insert(object_id); }
    while object_id as usize > session.objects.len() {
        let placeholder = session.objects.len();
        session.objects.push(objects::DocumentObject {
            kind: objects::ObjectKind::Unknown,
            matrix_bbox: [0, 0, 0, 0],
            rule_lattice: false,
            segment_indexes: Vec::new(),
            bbox: [0, 0, 0, 0],
            reading_index: placeholder,
            row_start: 0,
            row_stop: 0,
            column_start: 0,
            column_stop: 0,
            confidence: 0.0,
            evidence: vec!["missing-imported-stage-object"],
            logical_spans: None,
            local_matrix: None,
        });
    }
    if object_id as usize == session.objects.len() {
        session.objects.push(objects::DocumentObject {
            kind: object_kind_value,
            matrix_bbox: [object_rect.left as usize, object_rect.top as usize, object_rect.right as usize, object_rect.bottom as usize],
            rule_lattice: false,
            segment_indexes: segment_indexes.clone(),
            bbox: [
                object_rect.left as usize,
                object_rect.top as usize,
                object_rect.right as usize,
                object_rect.bottom as usize,
            ],
            reading_index: object_id as usize,
            row_start: 0,
            row_stop: logical_row_count.max(1) as usize,
            column_start: 0,
            column_stop: logical_column_count.max(1) as usize,
            confidence: 1.0,
            evidence: vec!["imported-stage-boundary"],
            logical_spans: None,
            local_matrix: None,
        });
    } else if let Some(object) = session.objects.get_mut(object_id as usize) {
        object.segment_indexes.extend(segment_indexes.iter().copied());
        object.segment_indexes.sort_unstable();
        object.segment_indexes.dedup();
    }
    session.blocks.push(blocks::RecognitionBlock {
        bbox: [
            block_rect.left as usize,
            block_rect.top as usize,
            block_rect.right as usize,
            block_rect.bottom as usize,
        ],
        core_segment_indexes,
        segment_indexes,
        context_segment_indexes,
        object_indexes: vec![object_id as usize],
        scope_index: object_id as usize,
        matrix_window,
        dyadic_mask: matrix_present,
        matrix_segment_shape: None,
        logical_segment_spans,
        logical_scope_shape: Some([
            logical_row_count.max(1) as usize,
            logical_column_count.max(1) as usize,
        ]),
    });
    session.block_rasters.push(raster.clone());
    session.pending_contexts.push_back(PendingContext { job, raster });
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_separated_import_ocr_job(
    handle: u32,
    block_index: u32,
    profile: u32,
    transform: u32,
    words_in_source_space: u32,
    text: *const u8,
    text_length: u32,
    grammar_milli: u32,
) -> i32 {
    if text.is_null() && text_length != 0 {
        return -1;
    }
    let profile = match profile {
        0 => LanguageProfileId::RusEng,
        1 => LanguageProfileId::Rus,
        2 => LanguageProfileId::Eng,
        3 => LanguageProfileId::ChiSim,
        4 => LanguageProfileId::Ell,
        5 => LanguageProfileId::Equ,
        _ => return -2,
    };
    let imported_composite = transform == 2;
    let transform = match transform {
        0 => OcrTransform::Raw,
        1 => OcrTransform::GammaDark,
        // A frozen composite result has already been mapped to its source block.
        2 if words_in_source_space != 0 => OcrTransform::Raw,
        _ => return -2,
    };
    let bytes = if text_length == 0 {
        &[][..]
    } else {
        // SAFETY: the caller owns this byte range for this call.
        unsafe { slice::from_raw_parts(text, text_length as usize) }
    };
    let Ok(text) = str::from_utf8(bytes) else {
        return -3;
    };
    let Ok(mut registry) = registry().lock() else {
        return -4;
    };
    let Some(session) = registry.sessions.get_mut(&handle) else {
        return -4;
    };
    if session.active_context.is_some() || session.stage_mask & (1 << 5) != 0 {
        return -5;
    }
    let Some(context) = session.pending_contexts.get(block_index as usize).cloned() else {
        return -6;
    };
    if words_in_source_space == 0 && context.raster.pixels.is_empty() { return -8; }
    let mut job = context.job;
    job.language_profile = profile;
    job.transform = transform;
    job.words_in_source_space = words_in_source_space != 0;
    job.imported_composite = imported_composite;
    let raster = if job.words_in_source_space {
        context.raster
    } else {
        raster_for_request(
            &context.raster,
            LanguageRequest { profile, transform },
            job.object_kind,
        )
    };
    let index = session.jobs.len();
    session.jobs.push(job);
    session.job_rasters.push(raster);
    session.text.push(Some(text.to_owned()));
    session.confidence_milli.push(grammar_milli.min(1_000));
    session.mean_word_confidence_milli.push(0);
    session.words.push(Vec::new());
    session.superseded.push(false);
    session.selected.push(true);
    session.importing_ocr = true;
    i32::try_from(index).unwrap_or(-7)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_import_ocr_finish(handle: u32) -> i32 {
    let Ok(mut registry) = registry().lock() else {
        return 0;
    };
    let Some(session) = registry.sessions.get_mut(&handle) else {
        return 0;
    };
    if session.jobs.is_empty() {
        return 0;
    }
    session.pending_contexts.clear();
    session.active_context = None;
    session.active_agenda = None;
    session.active_attempts.clear();
    session.importing_ocr = false;
    session.stage_mask |= 1 << 5;
    1
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_run_get_segment(handle: u32) -> i32 {
    let Ok(mut registry) = registry().lock() else {
        return 0;
    };
    let Some(session) = registry.sessions.get_mut(&handle) else {
        return 0;
    };
    if session.stage_mask & (1 << 5) == 0 || session.active_context.is_some() {
        return 0;
    }
    session.recognized_segments = Some(materialize_recognized_segments(session));
    finalize_recognized_layouts(session);
    session.stage_mask |= 1 << 6;
    1
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_segment_count(handle: u32) -> u32 {
    registry()
        .lock()
        .ok()
        .and_then(|registry| {
            registry
                .sessions
                .get(&handle)
                .and_then(|session| session.recognized_segments.as_ref())
                .map(|segments| segments.len() as u32)
        })
        .unwrap_or(0)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_segment_field(handle: u32, index: u32, field: u32) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    let Some(segment) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.recognized_segments.as_ref())
        .and_then(|segments| segments.get(index as usize))
    else {
        return -1;
    };
    let value = match field {
        0 => segment.object_id as usize,
        1 => segment.object_kind as usize,
        2 => segment.cell.row as usize,
        3 => segment.cell.column as usize,
        4 => segment.cell.row_span as usize,
        5 => segment.cell.column_span as usize,
        6 => segment.source_segment_indexes.len(),
        7 => segment.evidence_job_indexes.len(),
        _ => return -1,
    };
    i32::try_from(value).unwrap_or(-1)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_segment_source(
    handle: u32,
    index: u32,
    source_index: u32,
) -> i32 {
    registry()
        .lock()
        .ok()
        .and_then(|registry| {
            registry
                .sessions
                .get(&handle)
                .and_then(|session| session.recognized_segments.as_ref())
                .and_then(|segments| segments.get(index as usize))
                .and_then(|segment| segment.source_segment_indexes.get(source_index as usize))
                .and_then(|value| i32::try_from(*value).ok())
        })
        .unwrap_or(-1)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_segment_text_length(handle: u32, index: u32) -> u32 {
    registry()
        .lock()
        .ok()
        .and_then(|registry| {
            registry
                .sessions
                .get(&handle)
                .and_then(|session| session.recognized_segments.as_ref())
                .and_then(|segments| segments.get(index as usize))
                .and_then(|segment| u32::try_from(segment.text.len()).ok())
        })
        .unwrap_or(0)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_separated_segment_text_copy(
    handle: u32,
    index: u32,
    output: *mut u8,
    capacity: u32,
) -> i32 {
    if output.is_null() && capacity != 0 {
        return -1;
    }
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    let Some(text) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.recognized_segments.as_ref())
        .and_then(|segments| segments.get(index as usize))
        .map(|segment| segment.text.as_bytes())
    else {
        return -1;
    };
    if text.len() != capacity as usize {
        return -1;
    }
    if !text.is_empty() {
        // SAFETY: the caller promises capacity bytes at output.
        unsafe { std::ptr::copy_nonoverlapping(text.as_ptr(), output, text.len()) };
    }
    0
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_start_ocr(handle: u32) -> i32 {
    let Ok(mut registry) = registry().lock() else {
        return 0;
    };
    let Some(session) = registry.sessions.get_mut(&handle) else {
        return 0;
    };
    if session.active_context.is_some() || !session.jobs.is_empty() {
        return 1;
    }
    if session.pending_contexts.iter().any(|context| context.raster.pixels.is_empty()) { return 0; }
    start_next_context(session);
    // A valid page can contain only rules or no foreground. Completing OCR
    // without jobs is still a successful stage transition.
    i32::from(!session.jobs.is_empty() || session.stage_mask & (1 << 5) != 0)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_separated_begin(
    pixels: *const u8,
    pixel_length: u32,
    width: u32,
    height: u32,
    stride: u32,
    format: u32,
) -> u32 {
    // SAFETY: the stage-only constructor performs the same complete validation.
    let handle = unsafe {
        ittm_separated_plan_begin(pixels, pixel_length, width, height, stride, format)
    };
    if handle == 0 || ittm_separated_start_ocr(handle) != 1 {
        if handle != 0
            && let Ok(mut registry) = registry().lock()
        {
            registry.sessions.remove(&handle);
        }
        return 0;
    }
    handle
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_job_raster_field(handle: u32, index: u32, field: u32) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    let Some(raster) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.job_rasters.get(index as usize))
    else {
        return -1;
    };
    match field {
        0 => raster.width as i32,
        1 => raster.height as i32,
        2 => raster.stride as i32,
        3 => 3,
        _ => -1,
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_job_raster_length(handle: u32, index: u32) -> u32 {
    registry()
        .lock()
        .ok()
        .and_then(|registry| {
            registry
                .sessions
                .get(&handle)
                .and_then(|session| session.job_rasters.get(index as usize))
                .map(|raster| raster.pixels.len() as u32)
        })
        .unwrap_or(0)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_separated_job_raster_copy(
    handle: u32,
    index: u32,
    output: *mut u8,
    capacity: u32,
) -> i32 {
    if output.is_null() && capacity > 0 {
        return -1;
    }
    let Ok(registry) = registry().lock() else {
        return -2;
    };
    let Some(raster) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.job_rasters.get(index as usize))
    else {
        return -3;
    };
    if (capacity as usize) < raster.pixels.len() {
        return -4;
    }
    if !raster.pixels.is_empty() {
        // SAFETY: capacity was validated and the source/destination do not overlap.
        unsafe {
            raster.pixels.copy_to(std::slice::from_raw_parts_mut(output, raster.pixels.len()))
        };
    }
    raster.pixels.len() as i32
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_job_count(handle: u32) -> u32 {
    registry()
        .lock()
        .ok()
        .and_then(|registry| {
            registry
                .sessions
                .get(&handle)
                .map(|session| session.jobs.len() as u32)
        })
        .unwrap_or(0)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_job_text_length(handle: u32, index: u32) -> u32 {
    registry()
        .lock()
        .ok()
        .and_then(|registry| {
            registry
                .sessions
                .get(&handle)
                .and_then(|session| session.text.get(index as usize))
                .and_then(Option::as_ref)
                .and_then(|text| u32::try_from(text.len()).ok())
        })
        .unwrap_or(0)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_separated_job_text_copy(
    handle: u32,
    index: u32,
    output: *mut u8,
    capacity: u32,
) -> i32 {
    if output.is_null() && capacity > 0 {
        return -1;
    }
    let Ok(registry) = registry().lock() else {
        return -2;
    };
    let Some(text) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.text.get(index as usize))
        .and_then(Option::as_ref)
    else {
        return -3;
    };
    if text.len() > capacity as usize {
        return -4;
    }
    if !text.is_empty() {
        // SAFETY: the caller supplies at least `capacity` writable bytes.
        unsafe { std::ptr::copy_nonoverlapping(text.as_ptr(), output, text.len()) };
    }
    0
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_object_count(handle: u32) -> u32 {
    registry()
        .lock()
        .ok()
        .and_then(|registry| {
            registry
                .sessions
                .get(&handle)
                .map(|session| session.objects.len() as u32)
        })
        .unwrap_or(0)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_object_field(handle: u32, index: u32, field: u32) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    let Some(object) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.objects.get(index as usize))
    else {
        return -1;
    };
    let value = match field {
        0..=3 => object.bbox[field as usize],
        4 => match object.kind {
            objects::ObjectKind::Paragraph => OBJECT_PARAGRAPH as usize,
            objects::ObjectKind::List => OBJECT_LIST as usize,
            objects::ObjectKind::Table => OBJECT_TABLE as usize,
            objects::ObjectKind::Unknown => OBJECT_UNKNOWN as usize,
            objects::ObjectKind::Flow => OBJECT_FLOW as usize,
        },
        5 => object.segment_indexes.len(),
        6 => object.reading_index,
        7 => object.row_start,
        8 => object.row_stop,
        9 => object.column_start,
        10 => object.column_stop,
        _ => return -1,
    };
    i32::try_from(value).unwrap_or(-1)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_object_segment(
    handle: u32,
    object_index: u32,
    segment_index: u32,
) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    registry
        .sessions
        .get(&handle)
        .and_then(|session| session.objects.get(object_index as usize))
        .and_then(|object| object.segment_indexes.get(segment_index as usize))
        .and_then(|value| i32::try_from(*value).ok())
        .unwrap_or(-1)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_block_count(handle: u32) -> u32 {
    registry()
        .lock()
        .ok()
        .and_then(|registry| {
            registry
                .sessions
                .get(&handle)
                .map(|session| session.blocks.len() as u32)
        })
        .unwrap_or(0)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_block_field(handle: u32, index: u32, field: u32) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    let Some(block) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.blocks.get(index as usize))
    else {
        return -1;
    };
    let value = match field {
        0..=3 => block.bbox[field as usize],
        4 => block.scope_index,
        5 => block.segment_indexes.len(),
        6 => usize::from(block.dyadic_mask),
        7 => block.matrix_window.map_or(0, |window| window[0]),
        8 => block.matrix_window.map_or(0, |window| window[1]),
        9 => block.matrix_window.map_or(0, |window| window[2]),
        10 => block.matrix_window.map_or(0, |window| window[3]),
        11 => block.logical_scope_shape.map_or(0, |shape| shape[0]),
        12 => block.logical_scope_shape.map_or(0, |shape| shape[1]),
        _ => return -1,
    };
    i32::try_from(value).unwrap_or(-1)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_block_segment(
    handle: u32,
    block_index: u32,
    segment_index: u32,
) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    registry
        .sessions
        .get(&handle)
        .and_then(|session| session.blocks.get(block_index as usize))
        .and_then(|block| block.segment_indexes.get(segment_index as usize))
        .and_then(|value| i32::try_from(*value).ok())
        .unwrap_or(-1)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_block_raster_field(
    handle: u32,
    block_index: u32,
    field: u32,
) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    let Some(raster) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.block_rasters.get(block_index as usize))
    else {
        return -1;
    };
    match field {
        0 => raster.width as i32,
        1 => raster.height as i32,
        2 => raster.stride as i32,
        3 => 3,
        _ => -1,
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_block_raster_placement_count(
    handle: u32,
    block_index: u32,
) -> u32 {
    let Ok(registry) = registry().lock() else {
        return 0;
    };
    registry
        .sessions
        .get(&handle)
        .and_then(|session| session.block_rasters.get(block_index as usize))
        .map_or(0, |raster| raster.placements.len() as u32)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_block_raster_placement_field(
    handle: u32,
    block_index: u32,
    placement_index: u32,
    field: u32,
) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    let Some(placement) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.block_rasters.get(block_index as usize))
        .and_then(|raster| raster.placements.get(placement_index as usize))
    else {
        return -1;
    };
    let value = match field {
        0 => placement.source_rect.left,
        1 => placement.source_rect.top,
        2 => placement.source_rect.right,
        3 => placement.source_rect.bottom,
        4 => placement.crop_rect.left,
        5 => placement.crop_rect.top,
        6 => placement.crop_rect.right,
        7 => placement.crop_rect.bottom,
        8 => return i32::try_from(placement.segment_indexes.len()).unwrap_or(-1),
        _ => return -1,
    };
    i32::try_from(value).unwrap_or(-1)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_block_raster_placement_segment(
    handle: u32,
    block_index: u32,
    placement_index: u32,
    segment_index: u32,
) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    registry
        .sessions
        .get(&handle)
        .and_then(|session| session.block_rasters.get(block_index as usize))
        .and_then(|raster| raster.placements.get(placement_index as usize))
        .and_then(|placement| placement.segment_indexes.get(segment_index as usize))
        .and_then(|value| i32::try_from(*value).ok())
        .unwrap_or(-1)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_block_raster_length(handle: u32, block_index: u32) -> u32 {
    let Ok(registry) = registry().lock() else {
        return 0;
    };
    registry
        .sessions
        .get(&handle)
        .and_then(|session| session.block_rasters.get(block_index as usize))
        .and_then(|raster| u32::try_from(raster.pixels.len()).ok())
        .unwrap_or(0)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_separated_block_raster_copy(
    handle: u32,
    block_index: u32,
    output: *mut u8,
    output_length: u32,
) -> i32 {
    if output.is_null() {
        return -1;
    }
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    let Some(raster) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.block_rasters.get(block_index as usize))
    else {
        return -1;
    };
    if raster.pixels.len() != output_length as usize {
        return -1;
    }
    unsafe {
        raster.pixels.copy_to(std::slice::from_raw_parts_mut(output, raster.pixels.len()));
    }
    i32::try_from(raster.pixels.len()).unwrap_or(-1)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_topology_row_count(handle: u32) -> u32 {
    let Ok(registry) = registry().lock() else {
        return 0;
    };
    registry
        .sessions
        .get(&handle)
        .map_or(0, |session| session.topology.rows.len() as u32)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_topology_row_field(handle: u32, index: u32, field: u32) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    let Some(row) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.topology.rows.get(index as usize))
    else {
        return -1;
    };
    let value = match field {
        0 => row.top,
        1 => row.bottom,
        2 => row.source_rows.len(),
        3 => row.slots.len(),
        _ => return -1,
    };
    i32::try_from(value).unwrap_or(-1)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_topology_source_row(
    handle: u32,
    row_index: u32,
    source_index: u32,
) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    registry
        .sessions
        .get(&handle)
        .and_then(|session| session.topology.rows.get(row_index as usize))
        .and_then(|row| row.source_rows.get(source_index as usize))
        .and_then(|value| i32::try_from(*value).ok())
        .unwrap_or(-1)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_topology_slot_field(
    handle: u32,
    row_index: u32,
    slot_index: u32,
    field: u32,
) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    let Some(row) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.topology.rows.get(row_index as usize))
    else {
        return -1;
    };
    let Some(slot) = row.slots.get(slot_index as usize) else {
        return -1;
    };
    match field {
        0 => i32::try_from(slot.start).unwrap_or(-1),
        1 => i32::try_from(slot.end).unwrap_or(-1),
        2 => i32::from(*row.codes.get(slot_index as usize).unwrap_or(&0)),
        3 => i32::from(slot.value == topology::SpatialValue::Empty),
        _ => -1,
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_job_field(handle: u32, index: u32, field: u32) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    let Some(session) = registry.sessions.get(&handle) else {
        return -1;
    };
    let Some(job) = session.jobs.get(index as usize) else {
        return -1;
    };
    match field {
        0 => job.rect.left as i32,
        1 => job.rect.top as i32,
        2 => job.rect.right as i32,
        3 => job.rect.bottom as i32,
        4 => job.object_id as i32,
        5 => job.row as i32,
        6 => job.column as i32,
        7 => job.row_span as i32,
        8 => job.column_span as i32,
        9 => job.recognition_mode as i32,
        10 => job.object_kind as i32,
        11 => job.language_profile as i32,
        12 => job.transform as i32,
        13 => job.depth as i32,
        14 => job.logical_row_count as i32,
        15 => job.logical_column_count as i32,
        16 => session.confidence_milli[index as usize] as i32,
        17 => i32::from(session.superseded[index as usize]),
        _ => -1,
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_job_segment_count(handle: u32, index: u32) -> u32 {
    registry()
        .lock()
        .ok()
        .and_then(|registry| {
            registry
                .sessions
                .get(&handle)
                .and_then(|session| session.jobs.get(index as usize))
                .map(|job| job.segments.len() as u32)
        })
        .unwrap_or(0)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_job_segment_field(
    handle: u32,
    index: u32,
    segment_index: u32,
    field: u32,
) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    let Some(session) = registry.sessions.get(&handle) else {
        return -1;
    };
    let Some(job) = session.jobs.get(index as usize) else {
        return -1;
    };
    let segment_index = segment_index as usize;
    let (Some(segment), Some(cell), Some(raster)) = (
        job.segments.get(segment_index),
        job.segment_cells.get(segment_index),
        session.job_rasters.get(index as usize),
    ) else {
        return -1;
    };
    let placement = raster
        .placements
        .iter()
        .find(|placement| placement.segment_indexes.contains(&segment_index));
    match field {
        0 => segment.left as i32,
        1 => segment.top as i32,
        2 => segment.right as i32,
        3 => segment.bottom as i32,
        4 => cell.row as i32,
        5 => cell.column as i32,
        6 => cell.row_span as i32,
        7 => cell.column_span as i32,
        8 => placement.map_or(-1, |value| value.crop_rect.left as i32),
        9 => placement.map_or(-1, |value| value.crop_rect.top as i32),
        10 => placement.map_or(-1, |value| value.crop_rect.right as i32),
        11 => placement.map_or(-1, |value| value.crop_rect.bottom as i32),
        12 => placement.map_or(-1, |value| value.source_rect.left as i32),
        13 => placement.map_or(-1, |value| value.source_rect.top as i32),
        14 => placement.map_or(-1, |value| value.source_rect.right as i32),
        15 => placement.map_or(-1, |value| value.source_rect.bottom as i32),
        16 => i32::from(placement.is_some()),
        17 => job
            .source_segment_indexes
            .get(segment_index)
            .and_then(|value| i32::try_from(*value).ok())
            .unwrap_or(-1),
        _ => -1,
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_language_node_count(handle: u32) -> u32 {
    registry()
        .lock()
        .ok()
        .and_then(|registry| {
            registry
                .sessions
                .get(&handle)
                .map(|session| session.language_state.nodes().len() as u32)
        })
        .unwrap_or(0)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_language_node_field(handle: u32, index: u32, field: u32) -> i32 {
    let Ok(registry) = registry().lock() else {
        return -1;
    };
    let Some(session) = registry.sessions.get(&handle) else {
        return -1;
    };
    let Some(node) = session.language_state.nodes().get(index as usize) else {
        return -1;
    };
    match field {
        0 => node.profile as i32,
        1 => node.initial_rank as i32,
        2 => i32::try_from(node.quality_sum_percent).unwrap_or(i32::MAX),
        3 => i32::try_from(node.attempts).unwrap_or(i32::MAX),
        4 => i32::try_from(node.exact_grammar).unwrap_or(i32::MAX),
        5 => i32::try_from(node.garbage).unwrap_or(i32::MAX),
        6 => node.last_grammar_percent as i32,
        7 => i32::from(session.language_state.locked_profile == Some(node.profile)),
        8 => i32::from(
            session.language_state.locked_profile == Some(node.profile)
                && session.language_state.lock_is_provisional,
        ),
        _ => -1,
    }
}

unsafe fn add_ocr_word_evidence(
    handle: u32,
    index: u32,
    text_pointer: *const u8,
    text_length: u32,
    left: u32,
    top: u32,
    right: u32,
    bottom: u32,
    confidence_milli: u32,
    ranking_confidence_units: u32,
) -> i32 {
    if text_pointer.is_null() || text_length == 0 || left >= right || top >= bottom {
        return -1;
    }
    let text_bytes = unsafe { slice::from_raw_parts(text_pointer, text_length as usize) };
    let Ok(text) = str::from_utf8(text_bytes) else {
        return -2;
    };
    let Ok(mut registry) = registry().lock() else {
        return -3;
    };
    let Some(session) = registry.sessions.get_mut(&handle) else {
        return -4;
    };
    let index = index as usize;
    let Some(raster) = session.job_rasters.get(index) else {
        return -5;
    };
    let Some(job) = session.jobs.get(index) else {
        return -5;
    };
    let words_in_source_space = job.words_in_source_space;
    // Python composite outputs remain in the original packed crop extent, but
    // stage 06 interprets their coordinates relative to the source block origin.
    // Preserve both facts; do not clip or silently remap frozen OCR observations.
    let (maximum_width, maximum_height) = if words_in_source_space && !job.imported_composite {
        (
            job.rect.right.saturating_sub(job.rect.left),
            job.rect.bottom.saturating_sub(job.rect.top),
        )
    } else {
        (raster.width, raster.height)
    };
    if right > maximum_width
        || bottom > maximum_height
        || (session.text[index].is_some() && !session.importing_ocr)
    {
        return -6;
    }
    let rect = if words_in_source_space {
        Rect {
            left: job.rect.left.saturating_add(left),
            top: job.rect.top.saturating_add(top),
            right: job.rect.left.saturating_add(right),
            bottom: job.rect.top.saturating_add(bottom),
        }
    } else {
        Rect {
            left,
            top,
            right,
            bottom,
        }
    };
    session.words[index].push(OcrWordEvidence {
        text: text.to_owned(),
        rect,
        confidence_milli: confidence_milli.min(1_000),
        ranking_confidence_units: ranking_confidence_units.min(1_000_000),
    });
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_separated_add_ocr_word(
    handle: u32,
    index: u32,
    text_pointer: *const u8,
    text_length: u32,
    left: u32,
    top: u32,
    right: u32,
    bottom: u32,
    confidence_milli: u32,
) -> i32 {
    let confidence_milli = confidence_milli.min(1_000);
    unsafe {
        add_ocr_word_evidence(
            handle,
            index,
            text_pointer,
            text_length,
            left,
            top,
            right,
            bottom,
            confidence_milli,
            confidence_milli.saturating_mul(1_000),
        )
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_separated_add_ocr_word_ppm(
    handle: u32,
    index: u32,
    text_pointer: *const u8,
    text_length: u32,
    left: u32,
    top: u32,
    right: u32,
    bottom: u32,
    confidence_ppm: u32,
) -> i32 {
    let confidence_ppm = confidence_ppm.min(1_000_000);
    unsafe {
        add_ocr_word_evidence(
            handle,
            index,
            text_pointer,
            text_length,
            left,
            top,
            right,
            bottom,
            confidence_ppm.saturating_add(500) / 1_000,
            confidence_ppm,
        )
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_separated_set_ocr(
    handle: u32,
    index: u32,
    text_pointer: *const u8,
    text_length: u32,
    confidence_milli: u32,
) -> i32 {
    if text_pointer.is_null() && text_length > 0 {
        return -1;
    }
    let text_bytes = if text_length == 0 {
        &[][..]
    } else {
        // SAFETY: the caller retains a valid UTF-8 byte range for this call.
        unsafe { slice::from_raw_parts(text_pointer, text_length as usize) }
    };
    let Ok(text) = str::from_utf8(text_bytes) else {
        return -2;
    };
    let Ok(mut registry) = registry().lock() else {
        return -3;
    };
    let Some(session) = registry.sessions.get_mut(&handle) else {
        return -4;
    };
    let index = index as usize;
    if index >= session.text.len() {
        return -5;
    }
    if session.text[index].is_some() {
        return -6;
    }
    let normalized_text = text.to_owned();
    session.text[index] = Some(normalized_text.clone());
    if !session.active_attempts.contains(&index) {
        return -7;
    }
    let word_confidences = session.words[index]
        .iter()
        .map(|word| f64::from(word.ranking_confidence_units) / 1_000_000.0)
        .collect::<Vec<_>>();
    let mean_confidence_ppm = if session.words[index].is_empty() {
        0
    } else {
        session.words[index]
            .iter()
            .map(|word| u64::from(word.ranking_confidence_units))
            .sum::<u64>()
            .checked_div(session.words[index].len() as u64)
            .unwrap_or(0)
            .min(1_000_000) as u32
    };
    let mean_confidence_milli = if word_confidences.is_empty() {
        0
    } else {
        (word_confidences.iter().sum::<f64>() / word_confidences.len() as f64 * 1_000.0)
            .round_ties_even() as u32
    };
    session.mean_word_confidence_milli[index] = mean_confidence_milli;
    let assessment = grammar::assess_grammar(
        &normalized_text,
        session.jobs[index].language_profile.code(),
        &word_confidences,
    );
    session.confidence_milli[index] = u32::from(assessment.percent) * 10;
    if let Some((_, _, request)) = attempt_native_evidence(session, index, 3) {
        session.evidenced_native_profiles.insert(request.profile);
    }
    let _legacy_confidence_milli = confidence_milli;
    let (minority_confusables, malformed_punctuation, numeric_separators) =
        grammar::selection_evidence(&normalized_text);
    let selection_percent = i32::from(assessment.percent)
        - i32::try_from(minority_confusables.saturating_mul(4)).unwrap_or(i32::MAX)
        - i32::try_from(malformed_punctuation.saturating_mul(3)).unwrap_or(i32::MAX)
        + i32::try_from(numeric_separators.min(2)).unwrap_or(2);
    let observation = LanguageObservation::with_selection(
        u32::from(assessment.percent),
        mean_confidence_milli,
        mean_confidence_ppm,
        selection_percent,
        minority_confusables,
        malformed_punctuation,
        numeric_separators,
        normalized_text.chars().count(),
    );
    let Some(mut agenda) = session.active_agenda.take() else {
        return -8;
    };
    match agenda.observe(&mut session.language_state, observation) {
        LanguageAgendaResult::Request(request) => {
            if !append_attempt(session, request) {
                let winner_request = agenda
                    .best_request()
                    .unwrap_or(LanguageRequest {
                        profile: session.jobs[index].language_profile,
                        transform: session.jobs[index].transform,
                    });
                select_active_winner(session, winner_request);
                release_active_raster_pixels(session);
                session.active_context = None;
                session.active_attempts.clear();
                session.pending_contexts.clear();
                session.active_agenda = None;
            } else {
                session.active_agenda = Some(agenda);
            }
        }
        LanguageAgendaResult::Complete { winner_request, .. } => {
            select_active_winner(session, winner_request);
            if session
                .active_context
                .as_ref()
                .is_some_and(|context| context.job.object_kind == OBJECT_TABLE)
            {
                session.table_profile_discovery_complete = true;
            }
            release_active_raster_pixels(session);
            session.active_context = None;
            session.active_attempts.clear();
            start_next_context(session);
        }
        LanguageAgendaResult::Exhausted { winner_request, .. } => {
            let table_context = session
                .active_context
                .as_ref()
                .is_some_and(|context| context.job.object_kind == OBJECT_TABLE);
            let unit_count = session.active_context.as_ref().map_or(0, |context| {
                recursive_raster_placements(&context.job, &context.raster).len()
            });
            let native_request =
                native_request_with_evidence(session, if unit_count <= 1 { 50 } else { 3 }).filter(
                    |request| !winner_has_native_profile(session, winner_request, request.profile),
                );
            let native_patched = native_request.is_some_and(|request| {
                patch_missing_native_units(session, winner_request, request)
            });
            let unresolved_native = (!native_patched).then_some(native_request).flatten();
            let fused_request = (!table_context)
                .then(|| fuse_active_word_evidence(session))
                .flatten();
            let selected_request = fused_request.unwrap_or_else(|| {
                if unit_count <= 1 {
                    unresolved_native.unwrap_or(winner_request)
                } else {
                    winner_request
                }
            });
            select_active_winner(session, selected_request);
            if table_context {
                let _ =
                    split_active_context(session, unresolved_native.map(|request| request.profile));
                session.table_profile_discovery_complete = true;
            }
            release_active_raster_pixels(session);
            session.active_context = None;
            session.active_attempts.clear();
            start_next_context(session);
        }
    }
    session.rendered = None;
    0
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_render_length(handle: u32) -> u32 {
    let Ok(mut registry) = registry().lock() else {
        return 0;
    };
    let Some(session) = registry.sessions.get_mut(&handle) else {
        return 0;
    };
    let rendered = render_session(session);
    let length = rendered.len() as u32;
    session.rendered = Some(rendered);
    session.stage_mask = ALL_STAGE_MASK;
    length
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_separated_render_copy(
    handle: u32,
    output: *mut u8,
    capacity: u32,
) -> i32 {
    if output.is_null() && capacity > 0 {
        return -1;
    }
    let Ok(registry) = registry().lock() else {
        return -2;
    };
    let Some(rendered) = registry
        .sessions
        .get(&handle)
        .and_then(|session| session.rendered.as_ref())
    else {
        return -3;
    };
    if (capacity as usize) < rendered.len() {
        return -4;
    }
    if !rendered.is_empty() {
        // SAFETY: capacity was validated and the source/destination do not overlap.
        unsafe { std::ptr::copy_nonoverlapping(rendered.as_ptr(), output, rendered.len()) };
    }
    rendered.len() as i32
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_stage_mask(handle: u32) -> u32 {
    registry()
        .lock()
        .ok()
        .and_then(|registry| {
            registry
                .sessions
                .get(&handle)
                .map(|session| session.stage_mask)
        })
        .unwrap_or(0)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_separated_drop(handle: u32) -> i32 {
    registry()
        .lock()
        .ok()
        .and_then(|mut registry| registry.sessions.remove(&handle))
        .map(|_| 0)
        .unwrap_or(-1)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn white_page_with_two_lines() -> (Vec<u8>, u32, u32) {
        let width = 80_u32;
        let height = 60_u32;
        let mut pixels = vec![255_u8; (width * height) as usize];
        for y in 7..15 {
            for x in 8..55 {
                pixels[(y * width + x) as usize] = 0;
            }
        }
        for y in 40..48 {
            for x in 12..70 {
                pixels[(y * width + x) as usize] = 0;
            }
        }
        (pixels, width, height)
    }

    #[test]
    fn overlap_chain_uses_stable_line_order_without_a_cyclic_comparator() {
        let a = Rect { left: 20, top: 0, right: 30, bottom: 10 };
        let b = Rect { left: 10, top: 4, right: 20, bottom: 14 };
        let c = Rect { left: 0, top: 8, right: 10, bottom: 18 };
        // A/B and B/C overlap by 60%, A/C by 20%. Comparing each pair
        // independently used to produce A > B > C > A.
        let mut values = vec![(a,"A"),(b,"B"),(c,"C")];
        sort_in_reading_order(&mut values, |(rect,_)| *rect);
        assert_eq!(values.iter().map(|(_,s)|*s).collect::<Vec<_>>(),vec!["C","B","A"]);
        let mut many: Vec<_> = (0..100).flat_map(|id| [(a,id*3),(b,id*3+1),(c,id*3+2)]).collect();
        sort_in_reading_order(&mut many, |(rect,_)| *rect);
        assert_eq!(many.len(),300);
        assert_eq!(many.iter().map(|(_,id)|*id).collect::<BTreeSet<_>>(),(0..300).collect());
        assert_eq!(many[0].1,2);
        assert_eq!(many[99].1,299);
    }

    #[test]
    fn reading_order_clusters_ascenders_before_sorting_left_to_right() {
        let word = |text: &str, left, top, right, bottom| OcrWordEvidence {
            text: text.to_owned(), rect: Rect { left, top, right, bottom },
            confidence_milli: 950, ranking_confidence_units: 950_000,
        };
        assert_eq!(reading_order_text(&[
            word("Beta", 20, 0, 30, 12), word("Alpha", 0, 4, 15, 14),
            word("lower", 0, 30, 30, 40),
        ]), "Alpha Beta lower");
    }

    #[test]
    fn geometry_import_matches_raster_import_and_preserves_list_classification() {
        let metadata = [
            3, 0, OBJECT_LIST, 10, 20, 50, 80, 10, 20, 50, 80,
            0, 0, 0, 0, 0, 0, 1, 1, 1, 7,
            1, 7, 10, 20, 50, 80, 0, 0, 1, 1,
            0, 0, 0, 1,
        ];
        let pixels = vec![255_u8; 40 * 60 * 3];
        let mut outputs = Vec::new();
        for geometry_only in [false, true] {
            let handle = ittm_separated_import_blocks_begin();
            let status = unsafe {
                if geometry_only {
                    ittm_separated_import_block_geometry(handle, metadata.as_ptr(), metadata.len() as u32, 40, 60, 120)
                } else {
                    ittm_separated_import_block(handle, metadata.as_ptr(), metadata.len() as u32,
                        pixels.as_ptr(), pixels.len() as u32, 40, 60, 120)
                }
            };
            assert_eq!(status, 0);
            if geometry_only {
                assert_eq!(ittm_separated_start_ocr(handle), 0);
                assert_eq!(unsafe { ittm_separated_import_ocr_job(handle, 0, 2, 0, 0,
                    "text".as_ptr(), 4, 1000) }, -8);
            }
            let index = unsafe { ittm_separated_import_ocr_job(handle, 0, 2, 0, 1,
                "list".as_ptr(), 4, 1000) };
            assert_eq!(index, 0);
            for row in 0..6 {
                for (prefix, left) in [("A", 1), ("B", 21)] {
                    let text = format!("{prefix}{row}");
                    assert_eq!(unsafe { ittm_separated_add_ocr_word_ppm(handle, 0, text.as_ptr(),
                        text.len() as u32, left, row * 8 + 1, left + 8, row * 8 + 7, 950_000) }, 0);
                }
            }
            assert_eq!(ittm_separated_import_ocr_finish(handle), 1);
            assert_eq!(ittm_separated_run_get_segment(handle), 1);
            {
                let locked = registry().lock().unwrap();
                let segments = locked.sessions[&handle].recognized_segments.as_ref().unwrap();
                assert_eq!(segments.len(), 1);
                assert_eq!(segments[0].object_kind, OBJECT_PARAGRAPH);
                assert_eq!(segments[0].text, "A0 B0 A1 B1 A2 B2 A3 B3 A4 B4 A5 B5");
                outputs.push(segments.clone());
            }
            ittm_separated_drop(handle);
        }
        assert_eq!(outputs[0], outputs[1]);
    }

    #[test]
    fn table_vote_length_counts_unicode_characters_like_python() {
        assert_eq!(cell_text_by_job(&[
            ("ravens".to_owned(), 950_000, 0),
            ("асе".to_owned(), 950_000, 1),
        ]), "ravens");
    }

    #[test]
    fn imported_matrix_retains_text_without_sources_and_blank_sourced_cells() {
        let mut metadata = vec![
            2, 0, OBJECT_TABLE, 0, 0, 40, 10, 0, 0, 40, 10,
            0, 1, 0, 1, 0, 4, 1, 4,
            2, 7, 9, // core geometric sources
            2, // source-backed entries
            7, 0, 0, 10, 10, 0, 0, 1, 1,
            9, 30, 0, 40, 10, 0, 3, 1, 1,
            0, 0, // context and raster placements
            4, // complete logical matrix, including two source-free slots
        ];
        for column in 0..4 {
            metadata.extend([0, column, 1, 1, column * 10, 0, (column + 1) * 10, 10]);
            match column {
                0 => metadata.extend([1, 7]),
                3 => metadata.extend([1, 9]),
                _ => metadata.push(0),
            }
        }
        let pixels = vec![255_u8; 40 * 10 * 3];
        let handle = ittm_separated_import_blocks_begin();
        assert_ne!(handle, 0);
        unsafe {
            // A truncated matrix must fail before changing the session.
            assert_eq!(ittm_separated_import_block(handle, metadata.as_ptr(),
                metadata.len() as u32 - 1, pixels.as_ptr(), pixels.len() as u32, 40, 10, 120), -3);
            assert_eq!(ittm_separated_import_block(handle, metadata.as_ptr(),
                metadata.len() as u32, pixels.as_ptr(), pixels.len() as u32, 40, 10, 120), 0);
            let text = "Alpha inferred";
            let index = ittm_separated_import_ocr_job(handle, 0, 2, 0, 1,
                text.as_ptr(), text.len() as u32, 1000);
            assert_eq!(index, 0);
            for (text, left) in [("Alpha", 1), ("inferred", 11)] {
                assert_eq!(ittm_separated_add_ocr_word_ppm(handle, index as u32,
                    text.as_ptr(), text.len() as u32, left, 1, left + 8, 9, 950_000), 0);
            }
        }
        assert_eq!(ittm_separated_import_ocr_finish(handle), 1);
        assert_eq!(ittm_separated_run_get_segment(handle), 1);
        {
            let locked = registry().lock().unwrap();
            let segments = locked.sessions[&handle].recognized_segments.as_ref().unwrap();
            assert_eq!(segments.len(), 3);
            assert_eq!(segments.iter().map(|s| s.cell.column).collect::<Vec<_>>(), [0, 1, 3]);
            assert_eq!(segments[0].source_segment_indexes, [7]);
            assert_eq!(segments[1].text, "inferred");
            assert!(segments[1].source_segment_indexes.is_empty());
            assert_eq!(segments[2].source_segment_indexes, [9]);
            assert!(segments[2].text.is_empty());
        }
        ittm_separated_drop(handle);
    }

    #[test]
    fn table_recursion_matches_the_python_full_64_16_4_1_descent() {
        assert_eq!(recursive_chunk_size(0, 236, false), Some(64));
        assert_eq!(recursive_chunk_size(0, 59, false), Some(59));
        assert_eq!(recursive_chunk_size(1, 64, false), Some(16));
        assert_eq!(recursive_chunk_size(2, 16, false), None);
        assert_eq!(recursive_chunk_size(2, 16, true), Some(4));
        assert_eq!(recursive_chunk_size(3, 4, true), Some(1));
        assert_eq!(recursive_chunk_size(4, 1, true), None);
    }

    #[test]
    fn canonical_paragraph_preparation_scales_without_inventing_a_border() {
        let width = 100_u32;
        let height = 120_u32;
        let mut pixels = vec![255_u8; (width * height * 3) as usize];
        pixels[0..3].fill(0);
        let raster = raster_for_request(
            &JobRaster {
                width,
                height,
                stride: width * 3,
                pixels: pixels.into(),
                ocr_scale: 1,
                placements: vec![RasterPlacement {
                    segment_indexes: vec![0],
                    source_rect: Rect {
                        left: 10,
                        top: 20,
                        right: 110,
                        bottom: 140,
                    },
                    crop_rect: Rect {
                        left: 0,
                        top: 0,
                        right: width,
                        bottom: height,
                    },
                }],
            },
            LanguageRequest {
                profile: LanguageProfileId::RusEng,
                transform: OcrTransform::Raw,
            },
            OBJECT_PARAGRAPH,
        );
        assert_eq!((raster.width, raster.height, raster.stride), (500, 600, 1500));
        assert_eq!(raster.placements[0].source_rect, Rect { left: 10, top: 20, right: 110, bottom: 140 });
        assert_eq!(
            raster.placements[0].crop_rect,
            Rect {
                left: 0,
                top: 0,
                right: 500,
                bottom: 600,
            }
        );
    }

    #[test]
    fn exhausted_mixed_context_fuses_confident_words_by_geometry() {
        let word = |text: &str, left: u32, right: u32, confidence: u32| OcrWordEvidence {
            text: text.to_owned(),
            rect: Rect {
                left,
                top: 10,
                right,
                bottom: 30,
            },
            confidence_milli: confidence / 1_000,
            ranking_confidence_units: confidence,
        };
        let attempts = vec![
            (
                0,
                820,
                vec![
                    word("MIXED", 0, 50, 968_000),
                    word("Д", 60, 75, 928_000),
                    word("#X", 90, 130, 700_000),
                ],
            ),
            (
                1,
                530,
                vec![
                    word("MIXED", 0, 50, 920_000),
                    word("凡", 60, 75, 0),
                    word("中", 90, 125, 960_000),
                    word("文", 112, 130, 950_000),
                ],
            ),
        ];
        let (baseline, slots) = fuse_word_slots(&attempts).expect("fused slots");
        assert_eq!(baseline, 0);
        assert_eq!(render_fused_word_slots(&slots), "MIXED Д 中 文");
        assert_eq!(
            slots
                .iter()
                .map(|(_rect, _words, attempt)| *attempt)
                .collect::<BTreeSet<_>>(),
            BTreeSet::from([0, 1])
        );
    }

    #[test]
    fn valid_small_rasters_reach_the_block_boundary() {
        for (width, height, rectangles) in [
            (100_usize, 30_usize, vec![[10, 10, 90, 20]]),
            (80, 40, vec![[8, 7, 55, 11], [12, 24, 70, 28]]),
            (40, 40, vec![]),
        ] {
            let mut pixels = vec![255_u8; width * height];
            for [left, top, right, bottom] in rectangles {
                for y in top..bottom {
                    pixels[y * width + left..y * width + right].fill(0);
                }
            }
            let analysis = geometry::analyze_geometry(&pixels, width, height, width, 1)
                .expect("valid raster geometry");
            let topology = topology::build_physical_topology(&analysis)
                .expect("valid raster topology");
            let objects = objects::reconstruct_objects_with_topology(&analysis, &topology)
                .expect("valid raster objects");
            blocks::plan_blocks_with_topology(&analysis, &objects, &topology)
                .expect("valid raster blocks");
            verified_route_jobs(&pixels, width, height, width, 1)
                .expect("valid raster compaction");
            let handle = unsafe {
                ittm_separated_begin(pixels.as_ptr(), pixels.len() as u32,
                    width as u32, height as u32, width as u32, 1)
            };
            assert_ne!(handle, 0, "valid {width}x{height} raster session");
            assert_eq!(ittm_separated_job_count(handle), 0);
            assert_eq!(ittm_separated_render_length(handle), 0);
            assert_eq!(ittm_separated_stage_mask(handle), ALL_STAGE_MASK);
            assert_eq!(ittm_separated_drop(handle), 0);
        }
    }

    #[test]
    fn plans_the_same_explicit_stage_boundary_for_raster_lines() {
        let (pixels, width, height) = white_page_with_two_lines();
        let (jobs, rasters, _objects, _blocks, _topology, _matrices) =
            verified_route_jobs(&pixels, width as usize, height as usize, width as usize, 1)
                .expect("verified route");
        assert_eq!(jobs.len(), 2);
        assert_eq!(rasters.len(), jobs.len());
        assert!(jobs[0].rect.left <= 12, "jobs: {jobs:?}");
        assert!(jobs[0].rect.top <= 11, "jobs: {jobs:?}");
        assert!(jobs[1].rect.right >= 66, "planned jobs: {jobs:?}");
        assert!(jobs[1].rect.bottom >= 44, "jobs: {jobs:?}");
        assert!(jobs.iter().all(|job| job.object_kind == OBJECT_PARAGRAPH));
    }

    #[test]
    fn native_ffi_runs_all_eight_stages_and_renders_in_source_order() {
        let (pixels, width, height) = white_page_with_two_lines();
        // SAFETY: the source byte slice remains alive for the call.
        let handle = unsafe {
            ittm_separated_begin(
                pixels.as_ptr(),
                pixels.len() as u32,
                width,
                height,
                width,
                1,
            )
        };
        assert_ne!(handle, 0);
        assert_eq!(ittm_separated_job_count(handle), 1);
        assert_eq!(ittm_separated_stage_mask(handle), PLANNED_STAGE_MASK);
        let mut index = 0_u32;
        while index < ittm_separated_job_count(handle) {
            let text = if ittm_separated_job_field(handle, index, 4) == 0 {
                "first"
            } else {
                "second"
            };
            assert_eq!(
                unsafe {
                    ittm_separated_add_ocr_word(
                        handle,
                        index,
                        text.as_ptr(),
                        text.len() as u32,
                        0,
                        0,
                        1,
                        1,
                        1_000,
                    )
                },
                0,
            );
            // SAFETY: the string remains alive for the call.
            assert_eq!(
                unsafe {
                    ittm_separated_set_ocr(handle, index, text.as_ptr(), text.len() as u32, 1_000)
                },
                0,
            );
            index += 1;
        }
        assert_eq!(index, 2);
        let length = ittm_separated_render_length(handle);
        let mut output = vec![0_u8; length as usize];
        // SAFETY: output owns exactly the reported capacity.
        assert_eq!(
            unsafe { ittm_separated_render_copy(handle, output.as_mut_ptr(), length) },
            length as i32,
        );
        assert_eq!(String::from_utf8(output).unwrap(), "first\n\nsecond");
        assert_eq!(ittm_separated_stage_mask(handle), ALL_STAGE_MASK);
        assert_eq!(ittm_separated_drop(handle), 0);
    }

    #[test]
    fn dark_pages_use_the_same_foreground_planner() {
        let width = 60;
        let height = 20;
        let mut pixels = vec![12_u8; width * height];
        for y in 6..14 {
            for x in 9..45 {
                pixels[y * width + x] = 240;
            }
        }
        let (jobs, _rasters, _objects, _blocks, _topology, _matrices) =
            verified_route_jobs(&pixels, width, height, width, 1).expect("verified route");
        assert_eq!(jobs.len(), 1);
        let inverted = pixels.iter().map(|value| 255 - value).collect::<Vec<_>>();
        let (light_jobs, ..) = verified_route_jobs(&inverted, width, height, width, 1).unwrap();
        assert_eq!(jobs.len(), light_jobs.len());
        assert_eq!(jobs[0].rect, light_jobs[0].rect);
        assert_eq!(jobs[0].segments, light_jobs[0].segments);
        assert_eq!(jobs[0].source_segment_indexes, light_jobs[0].source_segment_indexes);
    }

    #[test]
    fn thin_residual_geometry_is_accounted_for_without_ocr_jobs() {
        let (width,height)=(60,20);
        let mut pixels=vec![255_u8;width*height];
        for y in 6..10 {for x in 9..45 {pixels[y*width+x]=0;}}
        let analysis=geometry::analyze_geometry(&pixels,width,height,width,1).unwrap();
        let topology=topology::build_physical_topology(&analysis).unwrap();
        let objects=objects::reconstruct_objects_with_topology(&analysis,&topology).unwrap();
        assert!(!objects.source_segment_indexes.is_empty());
        assert_eq!(objects.segment_ownership.len(),objects.source_segment_indexes.len());
        assert!(objects.objects.iter().all(|o|!o.is_recognizable()));
        let plan=blocks::plan_blocks_with_topology(&analysis,&objects,&topology).unwrap();
        assert!(plan.blocks.is_empty());
        assert!(plan.source_segment_indexes.is_empty());
    }

    #[test]
    fn low_grammar_list_context_keeps_the_full_context() {
        let width = 80_u32;
        let height = 32_u32;
        let mut pixels = vec![255_u8; (width * height) as usize];
        for y in 7..11 {
            for x in 8..55 {
                pixels[(y * width + x) as usize] = 0;
            }
        }
        for y in 16..20 {
            for x in 12..70 {
                pixels[(y * width + x) as usize] = 0;
            }
        }
        let handle = unsafe {
            ittm_separated_begin(
                pixels.as_ptr(),
                pixels.len() as u32,
                width,
                height,
                width,
                1,
            )
        };
        assert_eq!(ittm_separated_job_count(handle), 1);
        {
            let mut registry = registry().lock().unwrap();
            registry
                .sessions
                .get_mut(&handle)
                .unwrap()
                .active_context
                .as_mut()
                .unwrap()
                .job
                .object_kind = OBJECT_LIST;
        }
        let text = "uncertain";
        assert_eq!(
            unsafe { ittm_separated_set_ocr(handle, 0, text.as_ptr(), text.len() as u32, 960) },
            0,
        );
        assert_eq!(ittm_separated_job_count(handle), 2);
        let mut index = 1_u32;
        while index < ittm_separated_job_count(handle) {
            assert_eq!(ittm_separated_job_field(handle, index, 13), 0);
            assert_eq!(
                unsafe {
                    ittm_separated_set_ocr(handle, index, text.as_ptr(), text.len() as u32, 960)
                },
                0,
            );
            index += 1;
        }
        assert!(index >= 7);
        assert_eq!(ittm_separated_job_count(handle), index);
        assert_eq!(ittm_separated_job_field(handle, 0, 11), 0);
        assert_eq!(ittm_separated_job_field(handle, 1, 12), 1);
        assert_eq!(
            (0..index)
                .filter(|candidate| ittm_separated_job_field(handle, *candidate, 17) == 0)
                .count(),
            1
        );
        assert_eq!(ittm_separated_drop(handle), 0);
    }

    #[test]
    fn low_grammar_paragraph_keeps_the_best_full_context() {
        let width = 80_u32;
        let height = 32_u32;
        let mut pixels = vec![255_u8; (width * height) as usize];
        for y in 7..11 {
            for x in 8..55 {
                pixels[(y * width + x) as usize] = 0;
            }
        }
        for y in 16..20 {
            for x in 12..70 {
                pixels[(y * width + x) as usize] = 0;
            }
        }
        let handle = unsafe {
            ittm_separated_begin(
                pixels.as_ptr(),
                pixels.len() as u32,
                width,
                height,
                width,
                1,
            )
        };
        let text = "uncertain";
        let mut index = 0_u32;
        while index < ittm_separated_job_count(handle) {
            assert_eq!(ittm_separated_job_field(handle, index, 13), 0);
            assert_eq!(
                unsafe {
                    ittm_separated_set_ocr(handle, index, text.as_ptr(), text.len() as u32, 960)
                },
                0,
            );
            index += 1;
        }
        assert!(index >= 7);
        assert_eq!(ittm_separated_job_count(handle), index);
        assert_eq!(
            (0..index)
                .filter(|candidate| ittm_separated_job_field(handle, *candidate, 17) == 0)
                .count(),
            1
        );
        assert_eq!(ittm_separated_drop(handle), 0);
    }

    #[test]
    fn recursive_children_repack_the_normalized_parent_tiles() {
        let job = OcrJob {
            rect: Rect {
                left: 100,
                top: 10,
                right: 220,
                bottom: 20,
            },
            segments: vec![
                Rect {
                    left: 100,
                    top: 10,
                    right: 160,
                    bottom: 20,
                },
                Rect {
                    left: 160,
                    top: 10,
                    right: 220,
                    bottom: 20,
                },
            ],
            source_segment_indexes: vec![0, 1],
            segment_cells: vec![
                SegmentCell {
                    row: 0,
                    column: 0,
                    row_span: 1,
                    column_span: 1,
                },
                SegmentCell {
                    row: 1,
                    column: 0,
                    row_span: 1,
                    column_span: 1,
                },
            ],
            compact_segments: true,
            object_id: 0,
            row: 0,
            column: 0,
            row_span: 2,
            column_span: 1,
            recognition_mode: 0,
            object_kind: OBJECT_PARAGRAPH,
            depth: 0,
            language_profile: LanguageProfileId::RusEng,
            transform: OcrTransform::Raw,
            logical_row_count: 2,
            logical_column_count: 1,
            ocr_required: true,
            words_in_source_space: false,
        imported_composite: false,
        };
        let parent = JobRaster {
            width: 4,
            height: 1,
            stride: 12,
            pixels: vec![10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120].into(),
            ocr_scale: 1,
            placements: vec![RasterPlacement {
                segment_indexes: vec![0, 1],
                source_rect: job.rect,
                crop_rect: Rect {
                    left: 0,
                    top: 0,
                    right: 4,
                    bottom: 1,
                },
            }],
        };
        let expanded = recursive_raster_placements(&job, &parent);
        assert_eq!(expanded.len(), 2);
        assert_eq!(expanded[0].crop_rect.right, 2);
        assert_eq!(expanded[1].crop_rect.left, 2);
        let mapping = BTreeMap::from([(0, 0), (1, 1)]);
        let child = repack_parent_placements(&parent, &expanded, &mapping).unwrap();
        assert_eq!((child.width, child.height, child.stride), (2, 10, 6));
        assert_eq!(&child.pixels.decoded()[..6], &[10, 20, 30, 40, 50, 60]);
        assert_eq!(&child.pixels.decoded()[54..60], &[70, 80, 90, 100, 110, 120]);
        assert_eq!(child.placements[0].segment_indexes, vec![0]);
        assert_eq!(child.placements[1].segment_indexes, vec![1]);
        assert_eq!(
            child.placements[1].crop_rect,
            Rect {
                left: 0,
                top: 9,
                right: 2,
                bottom: 10,
            }
        );
    }

    #[test]
    fn table_word_boxes_map_back_to_source_cells() {
        let job = OcrJob {
            rect: Rect {
                left: 0,
                top: 0,
                right: 20,
                bottom: 10,
            },
            segments: vec![
                Rect {
                    left: 0,
                    top: 0,
                    right: 10,
                    bottom: 10,
                },
                Rect {
                    left: 10,
                    top: 0,
                    right: 20,
                    bottom: 10,
                },
            ],
            source_segment_indexes: vec![0, 1],
            segment_cells: vec![
                SegmentCell {
                    row: 0,
                    column: 0,
                    row_span: 1,
                    column_span: 1,
                },
                SegmentCell {
                    row: 0,
                    column: 1,
                    row_span: 1,
                    column_span: 1,
                },
            ],
            compact_segments: true,
            object_id: 0,
            row: 0,
            column: 0,
            row_span: 1,
            column_span: 2,
            recognition_mode: 0,
            object_kind: OBJECT_TABLE,
            depth: 0,
            language_profile: LanguageProfileId::RusEng,
            transform: OcrTransform::Raw,
            logical_row_count: 1,
            logical_column_count: 2,
            ocr_required: true,
            words_in_source_space: false,
        imported_composite: false,
        };
        let session = Session {
            objects: Vec::new(),
            blocks: Vec::new(),
            block_rasters: Vec::new(),
            topology: topology::PhysicalTopology { rows: Vec::new() },
            jobs: vec![job],
            job_rasters: vec![JobRaster {
                width: 20,
                height: 10,
                stride: 60,
                pixels: Vec::new().into(),
                ocr_scale: 1,
                placements: vec![RasterPlacement {
                    segment_indexes: vec![0, 1],
                    source_rect: Rect {
                        left: 0,
                        top: 0,
                        right: 20,
                        bottom: 10,
                    },
                    crop_rect: Rect {
                        left: 0,
                        top: 0,
                        right: 20,
                        bottom: 10,
                    },
                }],
            }],
            text: vec![Some("Alpha Beta".to_owned())],
            confidence_milli: vec![990],
            mean_word_confidence_milli: vec![975],
            words: vec![vec![
                OcrWordEvidence {
                    text: "Alpha".to_owned(),
                    rect: Rect {
                        left: 1,
                        top: 1,
                        right: 9,
                        bottom: 9,
                    },
                    confidence_milli: 980,
                    ranking_confidence_units: 980,
                },
                OcrWordEvidence {
                    text: "Beta".to_owned(),
                    rect: Rect {
                        left: 11,
                        top: 1,
                        right: 19,
                        bottom: 9,
                    },
                    confidence_milli: 970,
                    ranking_confidence_units: 970,
                },
            ]],
            superseded: vec![false],
            selected: vec![true],
            pending_contexts: VecDeque::new(),
            active_context: None,
            active_agenda: None,
            active_attempts: Vec::new(),
            language_state: LanguageSplayState::default(),
            evidenced_native_profiles: BTreeSet::new(),
            table_profile_discovery_complete: false,
            split_calibrations: BTreeMap::new(),
            active_split_probe: None,
            recognized_segments: None,
            recognized_layouts: BTreeMap::new(),
        matrix_cells: BTreeMap::new(),
        reading_order_objects: BTreeSet::new(),
            importing_ocr: false,
            rendered: None,
            stage_mask: ALL_STAGE_MASK,
        };
        assert_eq!(
            String::from_utf8(render_session(&session)).unwrap(),
            "| Alpha | Beta |\n| --- | --- |"
        );
        // Full-matrix materialization must undo OCR enlargement and shuffled
        // packed tiles before assigning words to original source cells.
        let mut session = session;
        session.matrix_cells.insert(0, vec![
            MatrixCell { cell: session.jobs[0].segment_cells[0], rect: session.jobs[0].segments[0], source_segment_indexes: vec![0] },
            MatrixCell { cell: session.jobs[0].segment_cells[1], rect: session.jobs[0].segments[1], source_segment_indexes: vec![1] },
        ]);
        session.job_rasters[0].width = 40;
        session.job_rasters[0].height = 20;
        session.job_rasters[0].stride = 120;
        session.job_rasters[0].ocr_scale = 2;
        session.job_rasters[0].placements = vec![
            RasterPlacement { segment_indexes: vec![0], source_rect: session.jobs[0].segments[0], crop_rect: Rect { left: 20, top: 0, right: 40, bottom: 20 } },
            RasterPlacement { segment_indexes: vec![1], source_rect: session.jobs[0].segments[1], crop_rect: Rect { left: 0, top: 0, right: 20, bottom: 20 } },
        ];
        session.words[0][0].rect = Rect { left: 23, top: 3, right: 37, bottom: 17 };
        session.words[0][1].rect = Rect { left: 3, top: 3, right: 17, bottom: 17 };
        let segments = materialize_recognized_segments(&session);
        assert_eq!(segments.iter().map(|s| (s.cell.column, s.text.as_str())).collect::<Vec<_>>(), [(0, "Alpha"), (1, "Beta")]);
        assert_eq!(word_center_in_source(&session.jobs[0], &session.job_rasters[0], &session.words[0][0]), (10, 10));
        // The already mapped Python05 path stays independent of packed geometry.
        session.jobs[0].words_in_source_space = true;
        session.words[0][0].rect = Rect { left: 1, top: 1, right: 9, bottom: 9 };
        session.words[0][1].rect = Rect { left: 11, top: 1, right: 19, bottom: 9 };
        assert_eq!(materialize_recognized_segments(&session), segments);
        // Python round(47 * (3 / 94)) is 1, whereas reassociation gives 2.
        session.jobs[0].words_in_source_space = false;
        session.job_rasters[0].ocr_scale = 1;
        session.job_rasters[0].placements = vec![RasterPlacement {
            segment_indexes: vec![0],
            source_rect: Rect { left: 0, top: 0, right: 3, bottom: 10 },
            crop_rect: Rect { left: 0, top: 0, right: 94, bottom: 10 },
        }];
        session.words[0][0].rect = Rect { left: 47, top: 1, right: 60, bottom: 9 };
        assert_eq!(word_center_in_source(&session.jobs[0], &session.job_rasters[0], &session.words[0][0]), (3, 10));
    }


}
