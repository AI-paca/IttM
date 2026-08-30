use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::slice;
use std::str;
use std::sync::{Mutex, OnceLock};

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
const SPLIT_CALIBRATION_SAMPLES: usize = 3;
const SPLIT_CALIBRATION_MIN_SUCCESSES: usize = 2;

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

#[derive(Clone, Debug, Eq, PartialEq)]
struct OcrWordEvidence {
    text: String,
    rect: Rect,
    confidence_milli: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct OcrJob {
    rect: Rect,
    segments: Vec<Rect>,
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
}

#[derive(Clone, Debug)]
struct JobRaster {
    width: u32,
    height: u32,
    stride: u32,
    pixels: Vec<u8>,
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
    topology: topology::PhysicalTopology,
    rendered: Option<Vec<u8>>,
    stage_mask: u32,
}

fn raster_for_request(raw: &JobRaster, request: LanguageRequest) -> JobRaster {
    let normalized = if request.transform == OcrTransform::Raw {
        normalize_dark_small_text_rgb(&raw.pixels, raw.width as usize, raw.height as usize)
    } else {
        gamma_dark_rgb(&raw.pixels, 3)
    };
    if normalized.is_none() {
        raw.clone()
    } else {
        let gray = normalized.unwrap_or_default();
        let mut pixels = Vec::with_capacity(gray.len().saturating_mul(3));
        for value in gray {
            pixels.extend_from_slice(&[value, value, value]);
        }
        JobRaster {
            width: raw.width,
            height: raw.height,
            stride: raw.width.saturating_mul(3),
            pixels,
            placements: raw.placements.clone(),
        }
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
    let raster = raster_for_request(&context.raster, request);
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
        return;
    };
    let evidenced_profiles = session
        .evidenced_native_profiles
        .iter()
        .copied()
        .collect::<Vec<_>>();
    let table_context = context.job.object_kind == OBJECT_TABLE;
    let agenda = if table_context && !session.table_profile_discovery_complete {
        LanguageAgenda::begin_for_context(&session.language_state, true)
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
            pixels[target..target + count].copy_from_slice(&parent.pixels[source..source + count]);
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
        pixels,
        placements,
    })
}

fn compact_context_children(
    parent: &OcrJob,
    parent_raster: &JobRaster,
    chunk_size: usize,
    child_depth: u32,
) -> Option<Vec<PendingContext>> {
    let recursive_placements = recursive_raster_placements(&parent, &parent_raster);
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

fn rect_reading_order(first: Rect, second: Rect) -> std::cmp::Ordering {
    let vertical_overlap = first
        .bottom
        .min(second.bottom)
        .saturating_sub(first.top.max(second.top));
    let minimum_height = first
        .bottom
        .saturating_sub(first.top)
        .min(second.bottom.saturating_sub(second.top));
    if vertical_overlap.saturating_mul(2) >= minimum_height.max(1) {
        (first.left, first.top, first.right, first.bottom).cmp(&(
            second.left,
            second.top,
            second.right,
            second.bottom,
        ))
    } else {
        (first.top, first.left, first.bottom, first.right).cmp(&(
            second.top,
            second.left,
            second.bottom,
            second.right,
        ))
    }
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
    let Some(placements) = session
        .active_context
        .as_ref()
        .map(|context| context.raster.placements.clone())
        .filter(|placements| !placements.is_empty())
    else {
        return false;
    };

    let mut replacements = BTreeMap::<usize, Vec<OcrWordEvidence>>::new();
    for word in session.words[native_index].iter().cloned() {
        if !native_only_word(&word.text, native_request.profile) {
            continue;
        }
        let Some((placement_index, area)) = placements
            .iter()
            .enumerate()
            .map(|(index, placement)| {
                (
                    index,
                    rect_intersection_area(word.rect, placement.crop_rect),
                )
            })
            .max_by_key(|(index, area)| (*area, std::cmp::Reverse(*index)))
        else {
            continue;
        };
        if area > 0 {
            replacements.entry(placement_index).or_default().push(word);
        }
    }
    replacements.retain(|_, words| {
        words
            .iter()
            .flat_map(|word| word.text.chars())
            .filter(|character| character.is_alphanumeric())
            .count()
            >= 2
    });
    if replacements.is_empty() {
        return false;
    }

    let winner_words = session.words[winner_index].clone();
    let winner_targets = winner_words
        .iter()
        .map(|word| {
            replacements
                .keys()
                .copied()
                .map(|index| {
                    (
                        index,
                        rect_intersection_area(word.rect, placements[index].crop_rect),
                    )
                })
                .max_by_key(|(index, area)| (*area, std::cmp::Reverse(*index)))
                .and_then(|(index, area)| (area > 0).then_some(index))
        })
        .collect::<Vec<_>>();
    let mut patched_native_words = Vec::new();
    for (placement_index, replacement_words) in replacements {
        for native in replacement_words {
            let matched = winner_words
                .iter()
                .enumerate()
                .filter(|(index, _)| winner_targets[*index] == Some(placement_index))
                .any(|(_index, word)| rect_intersection_area(native.rect, word.rect) > 0);
            if matched {
                patched_native_words.push(native);
            }
        }
    }
    if patched_native_words.is_empty() {
        return false;
    }
    let mut words = winner_words
        .into_iter()
        .filter(|word| {
            !patched_native_words
                .iter()
                .any(|native| rect_intersection_area(word.rect, native.rect) > 0)
        })
        .collect::<Vec<_>>();
    let mut seen = BTreeSet::new();
    words.extend(patched_native_words.into_iter().filter(|native| {
        seen.insert((
            native.text.clone(),
            native.rect.left,
            native.rect.top,
            native.rect.right,
            native.rect.bottom,
        ))
    }));
    words.sort_by(|first, second| rect_reading_order(first.rect, second.rect));
    let text = words
        .iter()
        .map(|word| word.text.as_str())
        .collect::<Vec<_>>()
        .join(" ");
    let confidences = words
        .iter()
        .map(|word| f64::from(word.confidence_milli) / 1_000.0)
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
        pixels: output,
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
)> {
    let analysis = geometry::analyze_geometry(pixels, width, height, stride, channels)?;
    let physical_topology = topology::build_physical_topology(&analysis)?;
    let reconstruction = objects::reconstruct_objects_with_topology(&analysis, &physical_topology)?;
    let plan = blocks::plan_blocks_with_topology(&analysis, &reconstruction, &physical_topology)?;
    if plan.blocks.len() > MAX_JOBS {
        return None;
    }
    let compacted = compact::compact_blocks(&analysis, &plan)?;
    if compacted.len() != plan.blocks.len() {
        return None;
    }
    let object_kind = |index: usize| match reconstruction.objects.get(index)?.kind {
        objects::ObjectKind::Paragraph => Some(OBJECT_PARAGRAPH),
        objects::ObjectKind::List => Some(OBJECT_LIST),
        objects::ObjectKind::Table => Some(OBJECT_TABLE),
        objects::ObjectKind::Unknown => Some(OBJECT_UNKNOWN),
    };
    let mut jobs = Vec::with_capacity(plan.blocks.len());
    let mut rasters = Vec::with_capacity(plan.blocks.len());
    for (block, compacted) in plan.blocks.iter().zip(compacted) {
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
        let document_object = reconstruction.objects.get(block.scope_index)?;
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
        let source_bbox = if matches!(
            document_object.kind,
            objects::ObjectKind::Paragraph | objects::ObjectKind::List
        ) {
            document_object.bbox
        } else {
            block.bbox
        };
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
        });
        let raster = if matches!(object_kind, OBJECT_PARAGRAPH | OBJECT_LIST) {
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
                pixels: compacted.pixels,
                placements,
            }
        };
        rasters.push(raster);
    }
    let reconstructed_objects = reconstruction.objects;
    let planned_blocks = plan.blocks;
    Some((
        jobs,
        rasters,
        reconstructed_objects,
        planned_blocks,
        physical_topology,
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
        let normalized = grammar::normalize_tesseract_text(text)
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
            segment_words.sort_by(|(_first_word, first), (_second_word, second)| {
                rect_reading_order(*first, *second)
            });
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
            segments.sort_by(|(first, _first_text), (second, _second_text)| {
                rect_reading_order(*first, *second)
            });
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

fn render_session(session: &Session) -> Vec<u8> {
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
    let mut objects: Vec<(u32, u32, Vec<usize>)> = by_object
        .into_iter()
        .map(|(object_id, mut indexes)| {
            indexes.sort_by_key(|index| {
                let job = &session.jobs[*index];
                (job.row, job.column, job.depth, *index)
            });
            let first_row = indexes
                .iter()
                .map(|index| session.jobs[*index].row)
                .min()
                .unwrap_or(0);
            (first_row, object_id, indexes)
        })
        .collect();
    objects.sort_by_key(|(first_row, object_id, _indexes)| (*first_row, *object_id));

    let mut rendered_objects = Vec::new();
    for (_first_row, object_id, indexes) in objects {
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

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_separated_begin(
    pixels: *const u8,
    pixel_length: u32,
    width: u32,
    height: u32,
    stride: u32,
    format: u32,
) -> u32 {
    let Some(channel_count) = channels(format) else {
        return 0;
    };
    let minimum_stride = match (width as usize).checked_mul(channel_count) {
        Some(value) => value,
        None => return 0,
    };
    let expected_length = match (stride as usize).checked_mul(height as usize) {
        Some(value) => value,
        None => return 0,
    };
    if pixels.is_null()
        || width == 0
        || height == 0
        || (stride as usize) < minimum_stride
        || pixel_length as usize != expected_length
    {
        return 0;
    }
    // SAFETY: the validated byte range is owned by the caller for this call.
    let input = unsafe { slice::from_raw_parts(pixels, expected_length) };
    let Some((jobs, job_rasters, objects, blocks, topology)) = verified_route_jobs(
        input,
        width as usize,
        height as usize,
        stride as usize,
        channel_count,
    ) else {
        return 0;
    };
    let block_rasters = job_rasters.clone();
    let mut pending_contexts = VecDeque::new();
    for (job, raster) in jobs.into_iter().zip(job_rasters) {
        if job.ocr_required {
            pending_contexts.push_back(PendingContext { job, raster });
        }
    }
    let mut session = Session {
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
        topology,
        rendered: None,
        stage_mask: PLANNED_STAGE_MASK,
    };
    start_next_context(&mut session);
    if session.jobs.is_empty() {
        return 0;
    }
    let Ok(mut registry) = registry().lock() else {
        return 0;
    };
    registry.next_handle = registry.next_handle.wrapping_add(1).max(1);
    let handle = registry.next_handle;
    registry.sessions.insert(handle, session);
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
            std::ptr::copy_nonoverlapping(raster.pixels.as_ptr(), output, raster.pixels.len())
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
        std::ptr::copy_nonoverlapping(raster.pixels.as_ptr(), output, raster.pixels.len());
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
    if right > raster.width || bottom > raster.height || session.text[index].is_some() {
        return -6;
    }
    session.words[index].push(OcrWordEvidence {
        text: text.to_owned(),
        rect: Rect {
            left,
            top,
            right,
            bottom,
        },
        confidence_milli: confidence_milli.min(1_000),
    });
    0
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
    let normalized_text = grammar::normalize_tesseract_text(text);
    session.text[index] = Some(normalized_text.clone());
    if !session.active_attempts.contains(&index) {
        return -7;
    }
    let word_confidences = session.words[index]
        .iter()
        .map(|word| f64::from(word.confidence_milli) / 1_000.0)
        .collect::<Vec<_>>();
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
    let observation =
        LanguageObservation::bounded(u32::from(assessment.percent), mean_confidence_milli);
    let Some(mut agenda) = session.active_agenda.take() else {
        return -8;
    };
    match agenda.observe(&mut session.language_state, observation) {
        LanguageAgendaResult::Request(request) => {
            session.active_agenda = Some(agenda);
            if !append_attempt(session, request) {
                return -9;
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
            session.active_context = None;
            session.active_attempts.clear();
            start_next_context(session);
        }
        LanguageAgendaResult::Exhausted { winner_request, .. } => {
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
            let selected_request = if unit_count <= 1 {
                unresolved_native.unwrap_or(winner_request)
            } else {
                winner_request
            };
            select_active_winner(session, selected_request);
            let recursive = session
                .active_context
                .as_ref()
                .is_some_and(|context| context.job.object_kind == OBJECT_TABLE);
            if recursive {
                let _ =
                    split_active_context(session, unresolved_native.map(|request| request.profile));
                session.table_profile_discovery_complete = true;
            }
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
        let height = 40_u32;
        let mut pixels = vec![255_u8; (width * height) as usize];
        for y in 7..11 {
            for x in 8..55 {
                pixels[(y * width + x) as usize] = 0;
            }
        }
        for y in 24..28 {
            for x in 12..70 {
                pixels[(y * width + x) as usize] = 0;
            }
        }
        (pixels, width, height)
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
    fn plans_the_same_explicit_stage_boundary_for_raster_lines() {
        let (pixels, width, height) = white_page_with_two_lines();
        let (jobs, rasters, _objects, _blocks, _topology) =
            verified_route_jobs(&pixels, width as usize, height as usize, width as usize, 1)
                .expect("verified route");
        assert_eq!(jobs.len(), 2);
        assert_eq!(rasters.len(), jobs.len());
        assert!(jobs[0].rect.left <= 8);
        assert!(jobs[0].rect.top <= 7);
        assert!(jobs[1].rect.right >= 70, "planned jobs: {jobs:?}");
        assert!(jobs[1].rect.bottom >= 28);
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
        for y in 6..10 {
            for x in 9..45 {
                pixels[y * width + x] = 240;
            }
        }
        let (jobs, _rasters, _objects, _blocks, _topology) =
            verified_route_jobs(&pixels, width, height, width, 1).expect("verified route");
        assert_eq!(jobs.len(), 1);
        assert!(jobs[0].rect.left <= 9);
        assert!(jobs[0].rect.right >= 45);
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
        for index in 1..7_u32 {
            assert_eq!(ittm_separated_job_field(handle, index, 13), 0);
            assert_eq!(
                unsafe {
                    ittm_separated_set_ocr(handle, index, text.as_ptr(), text.len() as u32, 960)
                },
                0,
            );
        }
        assert_eq!(ittm_separated_job_count(handle), 7);
        assert_eq!(ittm_separated_job_field(handle, 0, 11), 0);
        assert_eq!(ittm_separated_job_field(handle, 1, 12), 1);
        assert_eq!(
            (0..7)
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
        assert_eq!(index, 7);
        assert_eq!(ittm_separated_job_count(handle), 7);
        assert_eq!(
            (0..7)
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
        };
        let parent = JobRaster {
            width: 4,
            height: 1,
            stride: 12,
            pixels: vec![10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120],
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
        assert_eq!(&child.pixels[..6], &[10, 20, 30, 40, 50, 60]);
        assert_eq!(&child.pixels[54..60], &[70, 80, 90, 100, 110, 120]);
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
                pixels: Vec::new(),
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
            rendered: None,
            stage_mask: ALL_STAGE_MASK,
        };
        assert_eq!(
            String::from_utf8(render_session(&session)).unwrap(),
            "| Alpha | Beta |\n| --- | --- |"
        );
    }
}
