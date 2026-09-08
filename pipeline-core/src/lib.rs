mod assembler;
mod blocks;
mod candidates;
mod compact;
mod raster;
mod geometry;
mod grammar;
mod language;
mod local_structure;
mod objects;
mod object_matrix;
mod pdf_native;
mod separated;
mod topology;

pub use pdf_native::{
    NativePdfArtifact, NativePdfSegment, NativeTextCell, NativeTextObject, PdfTextGeometryItem,
    build_native_pdf_artifact,
};

pub use candidates::{SpanEvidence, score_span_evidence};

pub const ABI_VERSION: u32 = 6;
pub const SEPARATED_ROUTE_ID: u32 = 0x5253_0003;

pub const MERGE_UP_CODE: u8 = 3;
pub const MERGE_LEFT_CODE: u8 = 5;
pub const EMPTY_SLOT_CODE: u8 = 11;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum PixelFormat {
    Gray8 = 1,
    Rgb8 = 3,
    Rgba8 = 4,
}

impl PixelFormat {
    pub const fn channels(self) -> u32 {
        self as u32
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ImagePlane {
    pub width: u32,
    pub height: u32,
    pub stride: u32,
    pub format: PixelFormat,
    pub pixels: Vec<u8>,
}

impl ImagePlane {
    pub fn is_valid(&self) -> bool {
        let minimum_stride = self.width.saturating_mul(self.format.channels());
        self.width > 0
            && self.height > 0
            && self.stride >= minimum_stride
            && self.pixels.len() == self.stride.saturating_mul(self.height) as usize
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum Stage {
    Align = 0,
    Segment = 1,
    ProjectSparse = 2,
    RecognizeSegments = 3,
    SelectLanguageCandidate = 4,
    LexicalCorrection = 5,
    GroupStructures = 6,
    RenderMarkdown = 7,
}

impl Stage {
    pub const fn bit(self) -> u32 {
        1_u32 << self as u8
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PipelineCapabilities {
    pub trusted_text: bool,
    pub provides_layout: bool,
    pub provides_markdown: bool,
    pub needs_language_retry: bool,
}

impl Default for PipelineCapabilities {
    fn default() -> Self {
        Self {
            trusted_text: false,
            provides_layout: false,
            provides_markdown: false,
            needs_language_retry: true,
        }
    }
}

impl PipelineCapabilities {
    pub const TRUSTED_TEXT: u32 = 1 << 0;
    pub const PROVIDES_LAYOUT: u32 = 1 << 1;
    pub const PROVIDES_MARKDOWN: u32 = 1 << 2;
    pub const NEEDS_LANGUAGE_RETRY: u32 = 1 << 3;

    pub const fn from_bits(bits: u32) -> Self {
        Self {
            trusted_text: bits & Self::TRUSTED_TEXT != 0,
            provides_layout: bits & Self::PROVIDES_LAYOUT != 0,
            provides_markdown: bits & Self::PROVIDES_MARKDOWN != 0,
            needs_language_retry: bits & Self::NEEDS_LANGUAGE_RETRY != 0,
        }
    }
}

pub const fn recipe_mask(capabilities: PipelineCapabilities) -> u32 {
    let mut stages = Stage::RecognizeSegments.bit();
    if !capabilities.provides_layout {
        stages |= Stage::Align.bit() | Stage::Segment.bit() | Stage::ProjectSparse.bit();
    }
    if capabilities.needs_language_retry && !capabilities.trusted_text {
        stages |= Stage::SelectLanguageCandidate.bit();
    }
    if !capabilities.trusted_text {
        stages |= Stage::LexicalCorrection.bit();
    }
    if !capabilities.provides_markdown {
        stages |= Stage::GroupStructures.bit() | Stage::RenderMarkdown.bit();
    }
    stages
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SparseCodeError {
    UnknownCode(u8),
    UnknownSignal(u8),
}

const fn signal_mask(signal: u8) -> Option<u8> {
    match signal {
        MERGE_UP_CODE => Some(1 << 0),
        MERGE_LEFT_CODE => Some(1 << 1),
        EMPTY_SLOT_CODE => Some(1 << 2),
        _ => None,
    }
}

const fn component_mask(code: u8) -> Option<u8> {
    match code {
        0 => Some(0),
        3 => Some(1 << 0),
        5 => Some(1 << 1),
        8 => Some((1 << 0) | (1 << 1)),
        11 => Some(1 << 2),
        14 => Some((1 << 0) | (1 << 2)),
        16 => Some((1 << 1) | (1 << 2)),
        19 => Some((1 << 0) | (1 << 1) | (1 << 2)),
        _ => None,
    }
}

const fn code_from_mask(mask: u8) -> u8 {
    let mut code = 0;
    if mask & (1 << 0) != 0 {
        code += MERGE_UP_CODE;
    }
    if mask & (1 << 1) != 0 {
        code += MERGE_LEFT_CODE;
    }
    if mask & (1 << 2) != 0 {
        code += EMPTY_SLOT_CODE;
    }
    code
}

pub const fn add_sparse_signal(code: u8, signal: u8) -> Result<u8, SparseCodeError> {
    let Some(signal_component) = signal_mask(signal) else {
        return Err(SparseCodeError::UnknownSignal(signal));
    };
    let Some(components) = component_mask(code) else {
        return Err(SparseCodeError::UnknownCode(code));
    };
    Ok(code_from_mask(components | signal_component))
}

pub const fn is_isolated_heading(
    run_rows: u32,
    content_chars: u32,
    max_line_chars: u32,
    bounded_above: bool,
    bounded_below: bool,
) -> bool {
    run_rows > 0
        && run_rows <= 2
        && content_chars >= 6
        && max_line_chars < 90
        && bounded_above
        && bounded_below
}

pub const fn should_replace_primary(
    primary_chars: u32,
    fallback_chars: u32,
    primary_tokens: u32,
    retained_primary_tokens: u32,
) -> bool {
    if primary_chars < 10 {
        if primary_chars > 0
            && primary_chars <= 3
            && primary_tokens <= 1
            && retained_primary_tokens == 0
            && fallback_chars >= 6
            && (fallback_chars as u64) >= (primary_chars as u64) * 3
        {
            return true;
        }
        return fallback_chars >= 80;
    }

    if primary_tokens == 0 {
        return false;
    }

    // Work in u64 so hostile or corrupted C/WASM inputs cannot overflow the
    // 1.4x growth calculation. Adding 9 implements integer ceil(/ 10).
    let scaled_primary = ((primary_chars as u64) * 14 + 9) / 10;
    let minimum_fallback = if scaled_primary > 180 {
        scaled_primary
    } else {
        180
    };
    if (fallback_chars as u64) < minimum_fallback {
        return false;
    }

    let retained = if retained_primary_tokens > primary_tokens {
        primary_tokens
    } else {
        retained_primary_tokens
    };
    (retained as u64) * 5 >= (primary_tokens as u64) * 4
}

pub const fn should_drop_text_block(
    candidate_chars: u32,
    existing_chars: u32,
    shared_tokens: u32,
    candidate_tokens: u32,
    existing_tokens: u32,
    similarity_milli: u32,
) -> bool {
    if candidate_chars < 32 || existing_chars < 32 {
        return false;
    }

    let similarity = if similarity_milli > 1_000 {
        1_000
    } else {
        similarity_milli
    };
    if similarity >= 880 {
        return true;
    }

    if candidate_tokens == 0 || existing_tokens == 0 {
        return false;
    }

    // A shared-token count cannot exceed either block. Clamp it before
    // calculating coverage so invalid FFI inputs never manufacture overlap.
    // The decision is intentionally directional: the incoming candidate may
    // be a repeated heading/subset of an existing full segment.
    let smaller_block = if candidate_tokens < existing_tokens {
        candidate_tokens
    } else {
        existing_tokens
    };
    let shared = if shared_tokens > smaller_block {
        smaller_block
    } else {
        shared_tokens
    };
    (shared as u64) * 1_000 >= (candidate_tokens as u64) * 850
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct BoxI32 {
    pub left: i32,
    pub top: i32,
    pub right: i32,
    pub bottom: i32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SparseCode {
    pub row: u32,
    pub column: u32,
    pub code: u8,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Segment {
    pub id: u32,
    pub source_bbox: BoxI32,
    pub content_bbox: BoxI32,
    pub anchor: Option<(u32, u32)>,
    pub codes: Vec<SparseCode>,
    pub structural_only: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SparseProjection {
    pub rows: u32,
    pub columns: u32,
    pub x_tracks: Vec<i32>,
    pub codes: Vec<SparseCode>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RecognizedSegment {
    pub kind: String,
    pub bbox: BoxI32,
    pub parts: Vec<String>,
    pub anchor: Option<(u32, u32)>,
    pub codes: Vec<SparseCode>,
    pub list_marker: bool,
    pub content_left: Option<i32>,
    pub counters: Vec<(String, u32)>,
    pub flags: Vec<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PipelineStageTrace {
    pub version: u32,
    pub stage: Stage,
    pub input_count: u32,
    pub output_count: u32,
    pub flags: Vec<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StructuralRecord {
    pub kind: String,
    pub bbox: BoxI32,
    pub parts: Vec<String>,
    pub anchor: Option<(u32, u32)>,
    pub codes: Vec<SparseCode>,
    pub list_marker: bool,
    pub content_left: Option<i32>,
    pub flags: Vec<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StructuralRenderArtifact {
    pub parts: Vec<String>,
    pub flags: Vec<String>,
    pub bypassed: bool,
    pub lossy_merge_rejected: bool,
    pub lint_pass: Option<bool>,
    pub confirmed: bool,
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_pipeline_abi_version() -> u32 {
    ABI_VERSION
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_pipeline_route_id() -> u32 {
    SEPARATED_ROUTE_ID
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_geometry_foreground_field(
    pixels: *const u8,
    pixel_length: u32,
    width: u32,
    height: u32,
    stride: u32,
    channels: u32,
    field: u32,
) -> i64 {
    if pixels.is_null() || pixel_length == 0 {
        return -1;
    }
    let Ok(channels) = usize::try_from(channels) else {
        return -1;
    };
    let source = unsafe { std::slice::from_raw_parts(pixels, pixel_length as usize) };
    let Some(result) = geometry::analyze_foreground(
        source,
        width as usize,
        height as usize,
        stride as usize,
        channels,
    ) else {
        return -1;
    };
    match field {
        0 => i64::try_from(result.foreground_pixels()).unwrap_or(i64::MAX),
        1 => i64::from(result.background[0]),
        2 => i64::from(result.background[1]),
        3 => i64::from(result.background[2]),
        4 => i64::try_from(result.width).unwrap_or(i64::MAX),
        5 => i64::try_from(result.height).unwrap_or(i64::MAX),
        6 => i64::try_from(
            result
                .physical_foreground
                .iter()
                .filter(|pixel| **pixel)
                .count(),
        )
        .unwrap_or(i64::MAX),
        7 => match result.mode.as_str() {
            "physical" => 0,
            "physical-frame-interior" => 1,
            "physical-fallback" => 2,
            value if value.starts_with("adaptive-dual:") => 3,
            _ => 4,
        },
        8 => i64::from(result.correction_milli_degrees),
        9 => i64::try_from(result.stride).unwrap_or(i64::MAX),
        10 => match result.region_operation.as_str() {
            "identity" => 0,
            "region-projector-dewarp" => 1,
            "region-document-dewarp" => 2,
            _ => 3,
        },
        11 => i64::from(result.region_confidence_ppm),
        12 => i64::from(result.region_source_angle_milli_degrees),
        13 => i64::try_from(result.rule_evidence.iter().filter(|pixel| **pixel).count())
            .unwrap_or(i64::MAX),
        _ => -1,
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_geometry_foreground_copy(
    pixels: *const u8,
    pixel_length: u32,
    width: u32,
    height: u32,
    stride: u32,
    channels: u32,
    output: *mut u8,
    output_length: u32,
) -> i32 {
    if pixels.is_null() || output.is_null() || pixel_length == 0 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(pixels, pixel_length as usize) };
    let Some(result) = geometry::analyze_foreground(
        source,
        width as usize,
        height as usize,
        stride as usize,
        channels as usize,
    ) else {
        return -1;
    };
    let required = result.width.saturating_mul(result.height);
    if output_length as usize != required {
        return -1;
    }
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    for (slot, foreground) in target.iter_mut().zip(result.foreground) {
        *slot = u8::from(foreground);
    }
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_geometry_aligned_rgb_copy(
    pixels: *const u8,
    pixel_length: u32,
    width: u32,
    height: u32,
    stride: u32,
    channels: u32,
    output: *mut u8,
    output_length: u32,
) -> i32 {
    if pixels.is_null() || output.is_null() || pixel_length == 0 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(pixels, pixel_length as usize) };
    let Some(result) = geometry::analyze_foreground(
        source,
        width as usize,
        height as usize,
        stride as usize,
        channels as usize,
    ) else {
        return -1;
    };
    if output_length as usize != result.pixels.len() {
        return -1;
    }
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    target.copy_from_slice(&result.pixels);
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_geometry_rule_evidence_copy(
    pixels: *const u8,
    pixel_length: u32,
    width: u32,
    height: u32,
    stride: u32,
    channels: u32,
    output: *mut u8,
    output_length: u32,
) -> i32 {
    if pixels.is_null() || output.is_null() || pixel_length == 0 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(pixels, pixel_length as usize) };
    let Some(result) = geometry::analyze_foreground(
        source,
        width as usize,
        height as usize,
        stride as usize,
        channels as usize,
    ) else {
        return -1;
    };
    if output_length as usize != result.rule_evidence.len() {
        return -1;
    }
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    for (slot, foreground) in target.iter_mut().zip(result.rule_evidence) {
        *slot = u8::from(foreground);
    }
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_geometry_component_field(
    mask: *const u8,
    mask_length: u32,
    width: u32,
    height: u32,
    component_index: u32,
    field: u32,
) -> i64 {
    if mask.is_null() || mask_length == 0 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(mask, mask_length as usize) };
    let Some(components) = geometry::connected_components(source, width as usize, height as usize)
    else {
        return -1;
    };
    if field == 0 {
        return i64::try_from(components.len()).unwrap_or(i64::MAX);
    }
    let Some(component) = components.get(component_index as usize) else {
        return -1;
    };
    match field {
        1..=4 => i64::try_from(component.bbox[field as usize - 1]).unwrap_or(i64::MAX),
        5 => i64::try_from(component.pixels).unwrap_or(i64::MAX),
        _ => -1,
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_geometry_components_copy(
    mask: *const u8,
    mask_length: u32,
    width: u32,
    height: u32,
    output: *mut u32,
    output_length: u32,
) -> i32 {
    if mask.is_null() || output.is_null() || mask_length == 0 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(mask, mask_length as usize) };
    let Some(components) = geometry::connected_components(source, width as usize, height as usize)
    else {
        return -1;
    };
    if output_length as usize != components.len().saturating_mul(5) {
        return -1;
    }
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    for (row, component) in target.chunks_exact_mut(5).zip(components) {
        for (slot, value) in row[..4].iter_mut().zip(component.bbox) {
            let Ok(value) = u32::try_from(value) else {
                return -1;
            };
            *slot = value;
        }
        let Ok(pixels) = u32::try_from(component.pixels) else {
            return -1;
        };
        row[4] = pixels;
    }
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_geometry_component_runs_copy(
    mask: *const u8,
    mask_length: u32,
    width: u32,
    height: u32,
    output: *mut u32,
    output_length: u32,
) -> i64 {
    if mask.is_null() || output.is_null() || mask_length == 0 || output_length % 4 != 0 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(mask, mask_length as usize) };
    let Some((_components, runs)) =
        geometry::connected_component_runs(source, width as usize, height as usize)
    else {
        return -1;
    };
    if output_length as usize != runs.len().saturating_mul(4) {
        return -2;
    }
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    for (row, run) in target.chunks_exact_mut(4).zip(&runs) {
        row[0] = run.component_index as u32;
        row[1] = run.row as u32;
        row[2] = run.start as u32;
        row[3] = run.stop as u32;
    }
    i64::try_from(runs.len()).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_geometry_rule_candidate_copy(
    mask: *const u8,
    mask_length: u32,
    width: u32,
    height: u32,
    operation: u32,
    parameter: u32,
    output: *mut u8,
    output_length: u32,
) -> i32 {
    if mask.is_null() || output.is_null() || mask_length == 0 || output_length != mask_length {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(mask, mask_length as usize) };
    let result = match operation {
        0 => geometry::orientation_candidates(
            source,
            width as usize,
            height as usize,
            true,
            parameter as usize,
        ),
        1 => geometry::orientation_candidates(
            source,
            width as usize,
            height as usize,
            false,
            parameter as usize,
        ),
        2 => geometry::fill_short_gaps(
            source,
            width as usize,
            height as usize,
            true,
            parameter as usize,
        ),
        3 => geometry::fill_short_gaps(
            source,
            width as usize,
            height as usize,
            false,
            parameter as usize,
        ),
        _ => None,
    };
    let Some(result) = result else {
        return -1;
    };
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    target.copy_from_slice(&result);
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_geometry_color_rule_draft_field(
    rgb: *const u8,
    rgb_length: u32,
    width: u32,
    height: u32,
    draft_index: u32,
    field: u32,
) -> i64 {
    if rgb.is_null() || rgb_length == 0 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(rgb, rgb_length as usize) };
    let Some(drafts) = geometry::color_rule_drafts(source, width as usize, height as usize) else {
        return -1;
    };
    if field == 0 {
        return i64::try_from(drafts.len()).unwrap_or(i64::MAX);
    }
    let Some(draft) = drafts.get(draft_index as usize) else {
        return -1;
    };
    match field {
        1 => i64::from(!draft.horizontal),
        2..=5 => i64::try_from(draft.bbox[field as usize - 2]).unwrap_or(i64::MAX),
        _ => -1,
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_geometry_color_rule_drafts_copy(
    rgb: *const u8,
    rgb_length: u32,
    width: u32,
    height: u32,
    output: *mut u32,
    output_length: u32,
) -> i32 {
    if rgb.is_null() || output.is_null() || rgb_length == 0 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(rgb, rgb_length as usize) };
    let Some(drafts) = geometry::color_rule_drafts(source, width as usize, height as usize) else {
        return -1;
    };
    if output_length as usize != drafts.len().saturating_mul(5) {
        return -1;
    }
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    for (row, draft) in target.chunks_exact_mut(5).zip(drafts) {
        row[0] = u32::from(!draft.horizontal);
        for (slot, value) in row[1..].iter_mut().zip(draft.bbox) {
            let Ok(value) = u32::try_from(value) else {
                return -1;
            };
            *slot = value;
        }
    }
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_geometry_mask_rule_draft_field(
    mask: *const u8,
    mask_length: u32,
    width: u32,
    height: u32,
    field: u32,
) -> i64 {
    if mask.is_null() || mask_length == 0 || field != 0 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(mask, mask_length as usize) };
    let Some(drafts) = geometry::mask_rule_drafts(source, width as usize, height as usize) else {
        return -1;
    };
    i64::try_from(drafts.len()).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_geometry_mask_rule_drafts_copy(
    mask: *const u8,
    mask_length: u32,
    width: u32,
    height: u32,
    output: *mut u32,
    output_length: u32,
) -> i32 {
    if mask.is_null() || output.is_null() || mask_length == 0 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(mask, mask_length as usize) };
    let Some(drafts) = geometry::mask_rule_drafts(source, width as usize, height as usize) else {
        return -1;
    };
    if output_length as usize != drafts.len().saturating_mul(5) {
        return -1;
    }
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    for (row, draft) in target.chunks_exact_mut(5).zip(drafts) {
        row[0] = u32::from(!draft.horizontal);
        for (slot, value) in row[1..].iter_mut().zip(draft.bbox) {
            let Ok(value) = u32::try_from(value) else {
                return -1;
            };
            *slot = value;
        }
    }
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_local_structure_line_count(
    rgb: *const u8,
    rgb_length: u32,
    width: u32,
    height: u32,
) -> i64 {
    if rgb.is_null() || rgb_length == 0 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(rgb, rgb_length as usize) };
    let Some(result) =
        local_structure::detect_local_structures(source, width as usize, height as usize)
    else {
        return -1;
    };
    i64::try_from(result.lines.len()).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_local_structure_lines_copy(
    rgb: *const u8,
    rgb_length: u32,
    width: u32,
    height: u32,
    output: *mut u32,
    output_length: u32,
) -> i32 {
    if rgb.is_null() || output.is_null() || rgb_length == 0 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(rgb, rgb_length as usize) };
    let Some(result) =
        local_structure::detect_local_structures(source, width as usize, height as usize)
    else {
        return -1;
    };
    if output_length as usize != result.lines.len().saturating_mul(8) {
        return -1;
    }
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    for (row, line) in target.chunks_exact_mut(8).zip(&result.lines) {
        row[0] = u32::from(!line.horizontal);
        for (slot, value) in row[1..5].iter_mut().zip(line.bbox) {
            let Ok(value) = u32::try_from(value) else {
                return -1;
            };
            *slot = value;
        }
        let Ok(support) = u32::try_from(line.support_pixels) else {
            return -1;
        };
        row[5] = support;
        row[6] = (line.strength * 1_000_000.0).round() as u32;
        let Some(network_index) = result
            .networks
            .iter()
            .position(|network| network.lines.contains(line))
        else {
            return -1;
        };
        let Ok(network_index) = u32::try_from(network_index) else {
            return -1;
        };
        row[7] = network_index;
    }
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_local_structure_configured_line_count(
    rgb: *const u8,
    rgb_length: u32,
    width: u32,
    height: u32,
    minimum_contrast: u32,
    minimum_length: u32,
    maximum_gap: u32,
    maximum_line_thickness: u32,
    junction_tolerance: u32,
) -> i64 {
    if rgb.is_null() || rgb_length == 0 {
        return -1;
    }
    let Ok(minimum_contrast) = u8::try_from(minimum_contrast) else {
        return -1;
    };
    let source = unsafe { std::slice::from_raw_parts(rgb, rgb_length as usize) };
    let Some(result) = local_structure::detect_local_structures_with_config(
        source,
        width as usize,
        height as usize,
        local_structure::LocalStructureConfig {
            minimum_contrast,
            minimum_length: minimum_length as usize,
            maximum_gap: maximum_gap as usize,
            maximum_line_thickness: maximum_line_thickness as usize,
            junction_tolerance: junction_tolerance as usize,
        },
    ) else {
        return -1;
    };
    i64::try_from(result.lines.len()).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_local_structure_configured_lines_copy(
    rgb: *const u8,
    rgb_length: u32,
    width: u32,
    height: u32,
    minimum_contrast: u32,
    minimum_length: u32,
    maximum_gap: u32,
    maximum_line_thickness: u32,
    junction_tolerance: u32,
    output: *mut u32,
    output_length: u32,
) -> i32 {
    if rgb.is_null() || output.is_null() || rgb_length == 0 {
        return -1;
    }
    let Ok(minimum_contrast) = u8::try_from(minimum_contrast) else {
        return -1;
    };
    let source = unsafe { std::slice::from_raw_parts(rgb, rgb_length as usize) };
    let Some(result) = local_structure::detect_local_structures_with_config(
        source,
        width as usize,
        height as usize,
        local_structure::LocalStructureConfig {
            minimum_contrast,
            minimum_length: minimum_length as usize,
            maximum_gap: maximum_gap as usize,
            maximum_line_thickness: maximum_line_thickness as usize,
            junction_tolerance: junction_tolerance as usize,
        },
    ) else {
        return -1;
    };
    if output_length as usize != result.lines.len().saturating_mul(8) {
        return -1;
    }
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    for (row, line) in target.chunks_exact_mut(8).zip(&result.lines) {
        row[0] = u32::from(!line.horizontal);
        for (slot, value) in row[1..5].iter_mut().zip(line.bbox) {
            let Ok(value) = u32::try_from(value) else {
                return -1;
            };
            *slot = value;
        }
        let Ok(support) = u32::try_from(line.support_pixels) else {
            return -1;
        };
        row[5] = support;
        row[6] = (line.strength * 1_000_000.0).round() as u32;
        let Some(network_index) = result
            .networks
            .iter()
            .position(|network| network.lines.contains(line))
        else {
            return -1;
        };
        let Ok(network_index) = u32::try_from(network_index) else {
            return -1;
        };
        row[7] = network_index;
    }
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_local_line_band_row_count(
    rgb: *const u8,
    rgb_length: u32,
    width: u32,
    height: u32,
) -> i64 {
    if rgb.is_null() || rgb_length == 0 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(rgb, rgb_length as usize) };
    let Some(rows) = geometry::local_line_band_rows(source, width as usize, height as usize) else {
        return -1;
    };
    i64::try_from(rows.len()).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_local_line_band_rows_copy(
    rgb: *const u8,
    rgb_length: u32,
    width: u32,
    height: u32,
    output: *mut u32,
    output_length: u32,
) -> i32 {
    if rgb.is_null() || output.is_null() || rgb_length == 0 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(rgb, rgb_length as usize) };
    let Some(rows) = geometry::local_line_band_rows(source, width as usize, height as usize) else {
        return -1;
    };
    if output_length as usize != rows.len().saturating_mul(5) {
        return -1;
    }
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    for (target_row, source_row) in target.chunks_exact_mut(5).zip(rows) {
        target_row.copy_from_slice(&source_row);
    }
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_edge_open_grid_line_count(
    rgb: *const u8,
    rgb_length: u32,
    width: u32,
    height: u32,
) -> i64 {
    if rgb.is_null() || rgb_length == 0 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(rgb, rgb_length as usize) };
    let Some(indexes) = geometry::edge_open_grid_indexes(source, width as usize, height as usize)
    else {
        return -1;
    };
    i64::try_from(indexes.len()).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_edge_open_grid_lines_copy(
    rgb: *const u8,
    rgb_length: u32,
    width: u32,
    height: u32,
    output: *mut u32,
    output_length: u32,
) -> i32 {
    if rgb.is_null() || output.is_null() || rgb_length == 0 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(rgb, rgb_length as usize) };
    let Some(indexes) = geometry::edge_open_grid_indexes(source, width as usize, height as usize)
    else {
        return -1;
    };
    if output_length as usize != indexes.len() {
        return -1;
    }
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    target.copy_from_slice(&indexes);
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_local_rule_draft_count(
    rgb: *const u8,
    rgb_length: u32,
    width: u32,
    height: u32,
) -> i64 {
    if rgb.is_null() || rgb_length == 0 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(rgb, rgb_length as usize) };
    let Some(drafts) =
        geometry::local_structure_rule_drafts(source, width as usize, height as usize)
    else {
        return -1;
    };
    i64::try_from(drafts.len()).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_local_rule_drafts_copy(
    rgb: *const u8,
    rgb_length: u32,
    width: u32,
    height: u32,
    output: *mut u32,
    output_length: u32,
) -> i32 {
    if rgb.is_null() || output.is_null() || rgb_length == 0 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(rgb, rgb_length as usize) };
    let Some(drafts) =
        geometry::local_structure_rule_drafts(source, width as usize, height as usize)
    else {
        return -1;
    };
    if output_length as usize != drafts.len().saturating_mul(5) {
        return -1;
    }
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    for (row, draft) in target.chunks_exact_mut(5).zip(drafts) {
        row[0] = u32::from(!draft.horizontal);
        for (slot, value) in row[1..].iter_mut().zip(draft.bbox) {
            let Ok(value) = u32::try_from(value) else {
                return -1;
            };
            *slot = value;
        }
    }
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_full_rule_draft_count(
    mask: *const u8,
    mask_length: u32,
    rgb: *const u8,
    rgb_length: u32,
    width: u32,
    height: u32,
) -> i64 {
    if mask.is_null() || rgb.is_null() || mask_length == 0 || rgb_length == 0 {
        return -1;
    }
    let mask = unsafe { std::slice::from_raw_parts(mask, mask_length as usize) };
    let rgb = unsafe { std::slice::from_raw_parts(rgb, rgb_length as usize) };
    let Some(drafts) = geometry::full_rule_drafts(mask, rgb, width as usize, height as usize)
    else {
        return -1;
    };
    i64::try_from(drafts.len()).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_full_rule_drafts_copy(
    mask: *const u8,
    mask_length: u32,
    rgb: *const u8,
    rgb_length: u32,
    width: u32,
    height: u32,
    output: *mut u32,
    output_length: u32,
) -> i32 {
    if mask.is_null() || rgb.is_null() || output.is_null() || mask_length == 0 || rgb_length == 0 {
        return -1;
    }
    let mask = unsafe { std::slice::from_raw_parts(mask, mask_length as usize) };
    let rgb = unsafe { std::slice::from_raw_parts(rgb, rgb_length as usize) };
    let Some(drafts) = geometry::full_rule_drafts(mask, rgb, width as usize, height as usize)
    else {
        return -1;
    };
    if output_length as usize != drafts.len().saturating_mul(5) {
        return -1;
    }
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    for (row, draft) in target.chunks_exact_mut(5).zip(drafts) {
        row[0] = u32::from(!draft.horizontal);
        for (slot, value) in row[1..].iter_mut().zip(draft.bbox) {
            let Ok(value) = u32::try_from(value) else {
                return -1;
            };
            *slot = value;
        }
    }
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_materialized_rule_count(
    rule_evidence: *const u8,
    rule_evidence_length: u32,
    rgb: *const u8,
    rgb_length: u32,
    foreground: *const u8,
    foreground_length: u32,
    width: u32,
    height: u32,
) -> i64 {
    if rule_evidence.is_null()
        || rgb.is_null()
        || foreground.is_null()
        || rule_evidence_length == 0
        || rgb_length == 0
        || foreground_length == 0
    {
        return -1;
    }
    let rule_evidence =
        unsafe { std::slice::from_raw_parts(rule_evidence, rule_evidence_length as usize) };
    let rgb = unsafe { std::slice::from_raw_parts(rgb, rgb_length as usize) };
    let foreground = unsafe { std::slice::from_raw_parts(foreground, foreground_length as usize) };
    let Some((rules, _)) = geometry::materialize_full_rules(
        rule_evidence,
        rgb,
        foreground,
        width as usize,
        height as usize,
    ) else {
        return -1;
    };
    i64::try_from(rules.len()).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_materialized_rules_copy(
    rule_evidence: *const u8,
    rule_evidence_length: u32,
    rgb: *const u8,
    rgb_length: u32,
    foreground: *const u8,
    foreground_length: u32,
    width: u32,
    height: u32,
    output: *mut u32,
    output_length: u32,
) -> i32 {
    if rule_evidence.is_null()
        || rgb.is_null()
        || foreground.is_null()
        || output.is_null()
        || rule_evidence_length == 0
        || rgb_length == 0
        || foreground_length == 0
    {
        return -1;
    }
    let rule_evidence =
        unsafe { std::slice::from_raw_parts(rule_evidence, rule_evidence_length as usize) };
    let rgb = unsafe { std::slice::from_raw_parts(rgb, rgb_length as usize) };
    let foreground = unsafe { std::slice::from_raw_parts(foreground, foreground_length as usize) };
    let Some((rules, _)) = geometry::materialize_full_rules(
        rule_evidence,
        rgb,
        foreground,
        width as usize,
        height as usize,
    ) else {
        return -1;
    };
    if output_length as usize != rules.len().saturating_mul(7) {
        return -1;
    }
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    for (row, rule) in target.chunks_exact_mut(7).zip(rules) {
        row[0] = u32::from(!rule.summary.horizontal);
        for (slot, value) in row[1..5].iter_mut().zip(rule.summary.bbox) {
            let Ok(value) = u32::try_from(value) else {
                return -1;
            };
            *slot = value;
        }
        let Ok(pixels) = u32::try_from(rule.foreground_pixels) else {
            return -1;
        };
        row[5] = pixels;
        row[6] = (rule.strength * 1_000_000.0).round() as u32;
    }
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_materialized_rule_mask_copy(
    rule_evidence: *const u8,
    rule_evidence_length: u32,
    rgb: *const u8,
    rgb_length: u32,
    foreground: *const u8,
    foreground_length: u32,
    width: u32,
    height: u32,
    output: *mut u8,
    output_length: u32,
) -> i32 {
    if rule_evidence.is_null()
        || rgb.is_null()
        || foreground.is_null()
        || output.is_null()
        || rule_evidence_length == 0
        || rgb_length == 0
        || foreground_length == 0
    {
        return -1;
    }
    let rule_evidence =
        unsafe { std::slice::from_raw_parts(rule_evidence, rule_evidence_length as usize) };
    let rgb = unsafe { std::slice::from_raw_parts(rgb, rgb_length as usize) };
    let foreground = unsafe { std::slice::from_raw_parts(foreground, foreground_length as usize) };
    let Some((_, rule_mask)) = geometry::materialize_full_rules(
        rule_evidence,
        rgb,
        foreground,
        width as usize,
        height as usize,
    ) else {
        return -1;
    };
    if output_length as usize != rule_mask.len() {
        return -1;
    }
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    target.copy_from_slice(&rule_mask);
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_materialize_candidate_rules_copy(
    packed_drafts: *const u32,
    packed_drafts_length: u32,
    candidate_slices: *const u8,
    candidate_slices_length: u32,
    foreground: *const u8,
    foreground_length: u32,
    width: u32,
    height: u32,
    rule_output: *mut u32,
    rule_output_length: u32,
    mask_output: *mut u8,
    mask_output_length: u32,
) -> i64 {
    if packed_drafts_length % 7 != 0
        || foreground_length as usize != (width as usize).saturating_mul(height as usize)
        || mask_output_length != foreground_length
        || foreground.is_null()
        || mask_output.is_null()
        || (packed_drafts_length > 0 && packed_drafts.is_null())
        || (candidate_slices_length > 0 && candidate_slices.is_null())
        || (rule_output_length > 0 && rule_output.is_null())
    {
        return -1;
    }
    let packed_drafts = if packed_drafts_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(packed_drafts, packed_drafts_length as usize) }
    };
    let candidate_slices = if candidate_slices_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(candidate_slices, candidate_slices_length as usize) }
    };
    let mut drafts = Vec::<geometry::RuleCandidateDraft>::new();
    for row in packed_drafts.chunks_exact(7) {
        let bbox = [
            row[1] as usize,
            row[2] as usize,
            row[3] as usize,
            row[4] as usize,
        ];
        if bbox[0] >= bbox[2]
            || bbox[1] >= bbox[3]
            || bbox[2] > width as usize
            || bbox[3] > height as usize
        {
            return -1;
        }
        let area = (bbox[2] - bbox[0]).saturating_mul(bbox[3] - bbox[1]);
        let offset = row[6] as usize;
        let Some(source) = candidate_slices.get(offset..offset.saturating_add(area)) else {
            return -1;
        };
        let mut candidate = vec![0_u8; foreground_length as usize];
        let box_width = bbox[2] - bbox[0];
        for (local_row, y) in (bbox[1]..bbox[3]).enumerate() {
            candidate[y * width as usize + bbox[0]..y * width as usize + bbox[2]]
                .copy_from_slice(&source[local_row * box_width..(local_row + 1) * box_width]);
        }
        drafts.push(geometry::RuleCandidateDraft {
            summary: geometry::RuleDraftSummary {
                horizontal: row[0] == 0,
                bbox,
            },
            candidate_mask: std::sync::Arc::from(candidate),
            claim_full_bbox: row[5] != 0,
            partition_evidence: true,
        });
    }
    let foreground = unsafe { std::slice::from_raw_parts(foreground, foreground_length as usize) };
    let Some((rules, mask)) =
        geometry::materialize_candidate_rules(&drafts, foreground, width as usize, height as usize)
    else {
        return -1;
    };
    if rule_output_length as usize != rules.len().saturating_mul(7) {
        return -2;
    }
    let rule_output = if rule_output_length == 0 {
        &mut []
    } else {
        unsafe { std::slice::from_raw_parts_mut(rule_output, rule_output_length as usize) }
    };
    let mask_output =
        unsafe { std::slice::from_raw_parts_mut(mask_output, mask_output_length as usize) };
    for (row, rule) in rule_output.chunks_exact_mut(7).zip(&rules) {
        row[0] = u32::from(!rule.summary.horizontal);
        row[1..5].copy_from_slice(&rule.summary.bbox.map(|value| value as u32));
        row[5] = rule.foreground_pixels as u32;
        row[6] = (rule.strength * 1_000_000.0).round_ties_even() as u32;
    }
    mask_output.copy_from_slice(&mask);
    i64::try_from(rules.len()).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_diacritic_guard_copy(
    mask: *const u8,
    mask_length: u32,
    width: u32,
    height: u32,
    output: *mut u8,
    output_length: u32,
) -> i32 {
    if mask.is_null() || output.is_null() || mask_length == 0 || output_length != mask_length {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(mask, mask_length as usize) };
    let Some(guarded) = geometry::guard_diacritic_gaps(source, width as usize, height as usize)
    else {
        return -1;
    };
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    target.copy_from_slice(&guarded);
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_regular_row_grid_copy(
    mask: *const u8,
    mask_length: u32,
    width: u32,
    height: u32,
    output: *mut f64,
    output_length: u32,
) -> i32 {
    if mask.is_null() || output.is_null() || mask_length == 0 || output_length != 4 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(mask, mask_length as usize) };
    let Some(result) = geometry::estimate_regular_row_grid(source, width as usize, height as usize)
    else {
        return -1;
    };
    let Some(grid) = result else {
        return 0;
    };
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    target.copy_from_slice(&[
        grid.pitch as f64,
        grid.phase as f64,
        grid.row_count as f64,
        grid.score,
    ]);
    1
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_best_row_grid_boundary(
    mask: *const u8,
    mask_length: u32,
    width: u32,
    height: u32,
    left: u32,
    top: u32,
    right: u32,
    bottom: u32,
    pitch: u32,
    phase: u32,
    row_count: u32,
) -> i64 {
    if mask.is_null() || mask_length == 0 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(mask, mask_length as usize) };
    let Some(result) = geometry::best_row_grid_boundary(
        source,
        width as usize,
        height as usize,
        [left as usize, top as usize, right as usize, bottom as usize],
        Some(geometry::RegularRowGrid {
            pitch: pitch as usize,
            phase: phase as usize,
            row_count: row_count as usize,
            score: 0.0,
        }),
    ) else {
        return -1;
    };
    result
        .and_then(|coordinate| i64::try_from(coordinate).ok())
        .unwrap_or(-2)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_protected_upper_boundary(
    mask: *const u8,
    mask_length: u32,
    diacritic_guard: *const u8,
    diacritic_guard_length: u32,
    width: u32,
    height: u32,
    left: u32,
    top: u32,
    right: u32,
    bottom: u32,
    guard_start: u32,
    guard_stop: u32,
) -> i32 {
    if mask.is_null() || diacritic_guard.is_null() || mask_length == 0 {
        return -1;
    }
    let mask = unsafe { std::slice::from_raw_parts(mask, mask_length as usize) };
    let guard =
        unsafe { std::slice::from_raw_parts(diacritic_guard, diacritic_guard_length as usize) };
    match geometry::protected_upper_boundary(
        mask,
        Some(guard),
        width as usize,
        height as usize,
        [left as usize, top as usize, right as usize, bottom as usize],
        guard_start as usize,
        guard_stop as usize,
    ) {
        Some(value) => i32::from(value),
        None => -1,
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_has_body_evidence_on_both_sides(
    components: *const u32,
    components_length: u32,
    left: u32,
    top: u32,
    right: u32,
    bottom: u32,
    coordinate: u32,
    body_height: f64,
) -> i32 {
    if components.is_null() || components_length == 0 || components_length % 5 != 0 {
        return -1;
    }
    let packed = unsafe { std::slice::from_raw_parts(components, components_length as usize) };
    let components: Vec<geometry::ConnectedComponent> = packed
        .chunks_exact(5)
        .map(|row| geometry::ConnectedComponent {
            bbox: [
                row[0] as usize,
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
            ],
            pixels: row[4] as usize,
        })
        .collect();
    match geometry::has_body_evidence_on_both_sides(
        &components,
        [left as usize, top as usize, right as usize, bottom as usize],
        coordinate as usize,
        body_height,
    ) {
        Some(value) => i32::from(value),
        None => -1,
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_best_row_valley(
    mask: *const u8,
    mask_length: u32,
    diacritic_guard: *const u8,
    diacritic_guard_length: u32,
    components: *const u32,
    components_length: u32,
    width: u32,
    height: u32,
    left: u32,
    top: u32,
    right: u32,
    bottom: u32,
    body_height: f64,
    body_evidence_height: f64,
) -> i64 {
    if mask.is_null()
        || diacritic_guard.is_null()
        || components.is_null()
        || mask_length == 0
        || components_length == 0
        || components_length % 5 != 0
    {
        return -1;
    }
    let mask = unsafe { std::slice::from_raw_parts(mask, mask_length as usize) };
    let guard =
        unsafe { std::slice::from_raw_parts(diacritic_guard, diacritic_guard_length as usize) };
    let packed = unsafe { std::slice::from_raw_parts(components, components_length as usize) };
    let components: Vec<geometry::ConnectedComponent> = packed
        .chunks_exact(5)
        .map(|row| geometry::ConnectedComponent {
            bbox: [
                row[0] as usize,
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
            ],
            pixels: row[4] as usize,
        })
        .collect();
    let Some(result) = geometry::best_row_valley(
        mask,
        width as usize,
        height as usize,
        [left as usize, top as usize, right as usize, bottom as usize],
        body_height,
        body_evidence_height,
        &components,
        Some(guard),
    ) else {
        return -1;
    };
    result
        .and_then(|coordinate| i64::try_from(coordinate).ok())
        .unwrap_or(-2)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_supports_imbalanced_row_gap(
    components: *const u32,
    components_length: u32,
    left: u32,
    top: u32,
    right: u32,
    bottom: u32,
    gap_start: u32,
    gap_stop: u32,
    before_pixels: u32,
    after_pixels: u32,
) -> i32 {
    if components.is_null() || components_length == 0 || components_length % 5 != 0 {
        return -1;
    }
    let packed = unsafe { std::slice::from_raw_parts(components, components_length as usize) };
    let components: Vec<geometry::ConnectedComponent> = packed
        .chunks_exact(5)
        .map(|row| geometry::ConnectedComponent {
            bbox: [
                row[0] as usize,
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
            ],
            pixels: row[4] as usize,
        })
        .collect();
    match geometry::supports_imbalanced_row_gap(
        &components,
        [left as usize, top as usize, right as usize, bottom as usize],
        gap_start as usize,
        gap_stop as usize,
        before_pixels as usize,
        after_pixels as usize,
    ) {
        Some(value) => i32::from(value),
        None => -1,
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_supports_projected_column_gap(
    rules: *const u32,
    rules_length: u32,
    left: u32,
    top: u32,
    right: u32,
    bottom: u32,
    gap_start: u32,
    gap_stop: u32,
    body_height: f64,
) -> i32 {
    if rules.is_null() || rules_length == 0 || rules_length % 5 != 0 {
        return -1;
    }
    let packed = unsafe { std::slice::from_raw_parts(rules, rules_length as usize) };
    let rules: Vec<geometry::RuleDraftSummary> = packed
        .chunks_exact(5)
        .map(|row| geometry::RuleDraftSummary {
            horizontal: row[0] == 0,
            bbox: [
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
                row[4] as usize,
            ],
        })
        .collect();
    match geometry::supports_projected_column_gap(
        &rules,
        [left as usize, top as usize, right as usize, bottom as usize],
        gap_start as usize,
        gap_stop as usize,
        body_height,
    ) {
        Some(value) => i32::from(value),
        None => -1,
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_best_separator_copy(
    mask: *const u8,
    mask_length: u32,
    components: *const u32,
    components_length: u32,
    rules: *const u32,
    rules_length: u32,
    diacritic_guard: *const u8,
    diacritic_guard_length: u32,
    projected_rules: *const u32,
    projected_rules_length: u32,
    width: u32,
    height: u32,
    left: u32,
    top: u32,
    right: u32,
    bottom: u32,
    row_minimum_gap: u32,
    column_minimum_gap: u32,
    body_height: f64,
    allow_non_rule_columns: u32,
    prefer_rows: u32,
    output: *mut u32,
    output_length: u32,
) -> i32 {
    if mask.is_null()
        || diacritic_guard.is_null()
        || output.is_null()
        || mask_length == 0
        || output_length != 6
        || components_length % 5 != 0
        || rules_length % 5 != 0
        || projected_rules_length % 5 != 0
        || (components_length > 0 && components.is_null())
        || (rules_length > 0 && rules.is_null())
        || (projected_rules_length > 0 && projected_rules.is_null())
    {
        return -1;
    }
    let mask = unsafe { std::slice::from_raw_parts(mask, mask_length as usize) };
    let guard =
        unsafe { std::slice::from_raw_parts(diacritic_guard, diacritic_guard_length as usize) };
    let packed_components = if components_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(components, components_length as usize) }
    };
    let components: Vec<geometry::ConnectedComponent> = packed_components
        .chunks_exact(5)
        .map(|row| geometry::ConnectedComponent {
            bbox: [
                row[0] as usize,
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
            ],
            pixels: row[4] as usize,
        })
        .collect();
    let unpack_rules = |pointer: *const u32, length: u32| {
        let packed = if length == 0 {
            &[]
        } else {
            unsafe { std::slice::from_raw_parts(pointer, length as usize) }
        };
        packed
            .chunks_exact(5)
            .map(|row| geometry::RuleDraftSummary {
                horizontal: row[0] == 0,
                bbox: [
                    row[1] as usize,
                    row[2] as usize,
                    row[3] as usize,
                    row[4] as usize,
                ],
            })
            .collect::<Vec<_>>()
    };
    let rules = unpack_rules(rules, rules_length);
    let projected_rules = unpack_rules(projected_rules, projected_rules_length);
    let Some(result) = geometry::best_separator(
        mask,
        width as usize,
        height as usize,
        [left as usize, top as usize, right as usize, bottom as usize],
        row_minimum_gap as usize,
        column_minimum_gap as usize,
        &components,
        body_height,
        &rules,
        Some(guard),
        allow_non_rule_columns != 0,
        &projected_rules,
        prefer_rows != 0,
    ) else {
        return -1;
    };
    let Some(separator) = result else {
        return 0;
    };
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    target.copy_from_slice(&[
        u32::from(!separator.rows),
        separator.bbox[0] as u32,
        separator.bbox[1] as u32,
        separator.bbox[2] as u32,
        separator.bbox[3] as u32,
        u32::from(separator.rule_seam),
    ]);
    1
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_layout_partition_evidence_copy(
    mask: *const u8,
    mask_length: u32,
    components: *const u32,
    components_length: u32,
    runs: *const u32,
    runs_length: u32,
    width: u32,
    height: u32,
    maximum_evidence_components: u32,
    evidence_output: *mut u8,
    evidence_output_length: u32,
    component_output: *mut u32,
    component_output_length: u32,
) -> i64 {
    if mask.is_null()
        || evidence_output.is_null()
        || component_output.is_null()
        || mask_length == 0
        || evidence_output_length != mask_length
        || components_length % 5 != 0
        || runs_length % 4 != 0
        || (components_length > 0 && components.is_null())
        || (runs_length > 0 && runs.is_null())
    {
        return -1;
    }
    let mask = unsafe { std::slice::from_raw_parts(mask, mask_length as usize) };
    let packed_components = if components_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(components, components_length as usize) }
    };
    let components: Vec<geometry::ConnectedComponent> = packed_components
        .chunks_exact(5)
        .map(|row| geometry::ConnectedComponent {
            bbox: [
                row[0] as usize,
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
            ],
            pixels: row[4] as usize,
        })
        .collect();
    let packed_runs = if runs_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(runs, runs_length as usize) }
    };
    let runs: Vec<geometry::ConnectedComponentRun> = packed_runs
        .chunks_exact(4)
        .map(|row| geometry::ConnectedComponentRun {
            component_index: row[0] as usize,
            row: row[1] as usize,
            start: row[2] as usize,
            stop: row[3] as usize,
        })
        .collect();
    let Some((evidence, meaningful)) = geometry::layout_partition_evidence(
        mask,
        width as usize,
        height as usize,
        &components,
        &runs,
        maximum_evidence_components as usize,
    ) else {
        return -1;
    };
    if component_output_length as usize != meaningful.len().saturating_mul(5) {
        return -2;
    }
    let evidence_target =
        unsafe { std::slice::from_raw_parts_mut(evidence_output, evidence_output_length as usize) };
    evidence_target.copy_from_slice(&evidence);
    let component_target = unsafe {
        std::slice::from_raw_parts_mut(component_output, component_output_length as usize)
    };
    for (row, component) in component_target.chunks_exact_mut(5).zip(&meaningful) {
        row[0] = component.bbox[0] as u32;
        row[1] = component.bbox[1] as u32;
        row[2] = component.bbox[2] as u32;
        row[3] = component.bbox[3] as u32;
        row[4] = component.pixels as u32;
    }
    i64::try_from(meaningful.len()).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_recursive_partition_base_copy(
    mask: *const u8,
    mask_length: u32,
    components: *const u32,
    components_length: u32,
    runs: *const u32,
    runs_length: u32,
    rules: *const u32,
    rules_length: u32,
    diacritic_guard: *const u8,
    diacritic_guard_length: u32,
    nonstructural_boxes: *const u32,
    nonstructural_boxes_length: u32,
    width: u32,
    height: u32,
    max_nodes: u32,
    max_depth: u32,
    min_safe_gap: u32,
    row_grid_enabled: u32,
    row_grid_pitch: u32,
    row_grid_phase: u32,
    row_grid_count: u32,
    cover_separators: u32,
    working_mask_output: *mut u8,
    working_mask_output_length: u32,
    nodes_output: *mut u32,
    nodes_output_length: u32,
) -> i64 {
    if mask.is_null()
        || diacritic_guard.is_null()
        || working_mask_output.is_null()
        || nodes_output.is_null()
        || mask_length == 0
        || working_mask_output_length != mask_length
        || components_length % 5 != 0
        || runs_length % 4 != 0
        || rules_length % 5 != 0
        || nonstructural_boxes_length % 4 != 0
        || (components_length > 0 && components.is_null())
        || (runs_length > 0 && runs.is_null())
        || (rules_length > 0 && rules.is_null())
        || (nonstructural_boxes_length > 0 && nonstructural_boxes.is_null())
    {
        return -1;
    }
    let mask = unsafe { std::slice::from_raw_parts(mask, mask_length as usize) };
    let guard =
        unsafe { std::slice::from_raw_parts(diacritic_guard, diacritic_guard_length as usize) };
    let packed_components = if components_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(components, components_length as usize) }
    };
    let components: Vec<geometry::ConnectedComponent> = packed_components
        .chunks_exact(5)
        .map(|row| geometry::ConnectedComponent {
            bbox: [
                row[0] as usize,
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
            ],
            pixels: row[4] as usize,
        })
        .collect();
    let packed_runs = if runs_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(runs, runs_length as usize) }
    };
    let runs: Vec<geometry::ConnectedComponentRun> = packed_runs
        .chunks_exact(4)
        .map(|row| geometry::ConnectedComponentRun {
            component_index: row[0] as usize,
            row: row[1] as usize,
            start: row[2] as usize,
            stop: row[3] as usize,
        })
        .collect();
    let packed_rules = if rules_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(rules, rules_length as usize) }
    };
    let rules: Vec<geometry::RuleDraftSummary> = packed_rules
        .chunks_exact(5)
        .map(|row| geometry::RuleDraftSummary {
            horizontal: row[0] == 0,
            bbox: [
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
                row[4] as usize,
            ],
        })
        .collect();
    let packed_boxes = if nonstructural_boxes_length == 0 {
        &[]
    } else {
        unsafe {
            std::slice::from_raw_parts(nonstructural_boxes, nonstructural_boxes_length as usize)
        }
    };
    let boxes: Vec<[usize; 4]> = packed_boxes
        .chunks_exact(4)
        .map(|row| {
            [
                row[0] as usize,
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
            ]
        })
        .collect();
    let row_grid = (row_grid_enabled != 0).then_some(geometry::RegularRowGrid {
        pitch: row_grid_pitch as usize,
        phase: row_grid_phase as usize,
        row_count: row_grid_count as usize,
        score: 0.0,
    });
    let Some((nodes, working_mask)) = geometry::recursive_partition_without_adaptive_rgb(
        mask,
        width as usize,
        height as usize,
        &components,
        &runs,
        geometry::PartitionConfig {
            max_nodes: max_nodes as usize,
            max_depth: max_depth as usize,
            min_safe_gap: min_safe_gap as usize,
        },
        &rules,
        Some(guard),
        row_grid,
        &boxes,
        cover_separators != 0,
    ) else {
        return -1;
    };
    let required_node_values = nodes.len().saturating_mul(19);
    if (nodes_output_length as usize) < required_node_values {
        return -2;
    }
    let mask_target = unsafe {
        std::slice::from_raw_parts_mut(working_mask_output, working_mask_output_length as usize)
    };
    mask_target.copy_from_slice(&working_mask);
    let node_target = unsafe { std::slice::from_raw_parts_mut(nodes_output, required_node_values) };
    for (row, node) in node_target.chunks_exact_mut(19).zip(&nodes) {
        row[0..4].copy_from_slice(&node.bbox.map(|value| value as u32));
        row[4] = node.depth as u32;
        row[5] = node.parent_index.map_or(0, |value| value as u32 + 1);
        row[6] = node.rows.map_or(0, |rows| if rows { 1 } else { 2 });
        row[7] = node.child_indexes[0].map_or(0, |value| value as u32 + 1);
        row[8] = node.child_indexes[1].map_or(0, |value| value as u32 + 1);
        if let Some(separator) = node.separator {
            row[9] = 1;
            row[10..14].copy_from_slice(&separator.map(|value| value as u32));
        }
        row[14] = node.split_coordinate.map_or(0, |value| value as u32 + 1);
        row[15] = match node.stop_reason {
            None => 0,
            Some(geometry::PartitionStopReason::Atomic) => 1,
            Some(geometry::PartitionStopReason::Empty) => 2,
            Some(geometry::PartitionStopReason::Limit) => 3,
        };
        row[16] = u32::from(node.rule_partition);
        row[17] = u32::from(node.row_rule_top);
        row[18] = u32::from(node.row_rule_bottom);
    }
    i64::try_from(nodes.len()).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_recursive_partition_adaptive_copy(
    mask: *const u8,
    mask_length: u32,
    partition_rgb: *const u8,
    partition_rgb_length: u32,
    physical_fallback: *const u8,
    physical_fallback_length: u32,
    components: *const u32,
    components_length: u32,
    runs: *const u32,
    runs_length: u32,
    rules: *const u32,
    rules_length: u32,
    diacritic_guard: *const u8,
    diacritic_guard_length: u32,
    nonstructural_boxes: *const u32,
    nonstructural_boxes_length: u32,
    width: u32,
    height: u32,
    max_nodes: u32,
    max_depth: u32,
    min_safe_gap: u32,
    row_grid_enabled: u32,
    row_grid_pitch: u32,
    row_grid_phase: u32,
    row_grid_count: u32,
    cover_separators: u32,
    working_mask_output: *mut u8,
    working_mask_output_length: u32,
    nodes_output: *mut u32,
    nodes_output_length: u32,
) -> i64 {
    if mask.is_null()
        || partition_rgb.is_null()
        || diacritic_guard.is_null()
        || working_mask_output.is_null()
        || nodes_output.is_null()
        || mask_length == 0
        || partition_rgb_length == 0
        || working_mask_output_length != mask_length
        || components_length % 5 != 0
        || runs_length % 4 != 0
        || rules_length % 5 != 0
        || nonstructural_boxes_length % 4 != 0
        || (physical_fallback_length > 0 && physical_fallback.is_null())
        || (components_length > 0 && components.is_null())
        || (runs_length > 0 && runs.is_null())
        || (rules_length > 0 && rules.is_null())
        || (nonstructural_boxes_length > 0 && nonstructural_boxes.is_null())
    {
        return -1;
    }
    let mask = unsafe { std::slice::from_raw_parts(mask, mask_length as usize) };
    let rgb = unsafe { std::slice::from_raw_parts(partition_rgb, partition_rgb_length as usize) };
    let fallback = if physical_fallback_length == 0 {
        None
    } else {
        Some(unsafe {
            std::slice::from_raw_parts(physical_fallback, physical_fallback_length as usize)
        })
    };
    let guard =
        unsafe { std::slice::from_raw_parts(diacritic_guard, diacritic_guard_length as usize) };
    let packed_components = if components_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(components, components_length as usize) }
    };
    let components: Vec<geometry::ConnectedComponent> = packed_components
        .chunks_exact(5)
        .map(|row| geometry::ConnectedComponent {
            bbox: [
                row[0] as usize,
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
            ],
            pixels: row[4] as usize,
        })
        .collect();
    let packed_runs = if runs_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(runs, runs_length as usize) }
    };
    let runs: Vec<geometry::ConnectedComponentRun> = packed_runs
        .chunks_exact(4)
        .map(|row| geometry::ConnectedComponentRun {
            component_index: row[0] as usize,
            row: row[1] as usize,
            start: row[2] as usize,
            stop: row[3] as usize,
        })
        .collect();
    let packed_rules = if rules_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(rules, rules_length as usize) }
    };
    let rules: Vec<geometry::RuleDraftSummary> = packed_rules
        .chunks_exact(5)
        .map(|row| geometry::RuleDraftSummary {
            horizontal: row[0] == 0,
            bbox: [
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
                row[4] as usize,
            ],
        })
        .collect();
    let packed_boxes = if nonstructural_boxes_length == 0 {
        &[]
    } else {
        unsafe {
            std::slice::from_raw_parts(nonstructural_boxes, nonstructural_boxes_length as usize)
        }
    };
    let boxes: Vec<[usize; 4]> = packed_boxes
        .chunks_exact(4)
        .map(|row| {
            [
                row[0] as usize,
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
            ]
        })
        .collect();
    let row_grid = (row_grid_enabled != 0).then_some(geometry::RegularRowGrid {
        pitch: row_grid_pitch as usize,
        phase: row_grid_phase as usize,
        row_count: row_grid_count as usize,
        score: 0.0,
    });
    let Some((nodes, working_mask)) = geometry::recursive_partition_with_adaptive_rgb(
        mask,
        width as usize,
        height as usize,
        &components,
        &runs,
        geometry::PartitionConfig {
            max_nodes: max_nodes as usize,
            max_depth: max_depth as usize,
            min_safe_gap: min_safe_gap as usize,
        },
        &rules,
        Some(guard),
        row_grid,
        &boxes,
        cover_separators != 0,
        rgb,
        fallback,
    ) else {
        return -1;
    };
    let required_node_values = nodes.len().saturating_mul(19);
    if (nodes_output_length as usize) < required_node_values {
        return -2;
    }
    let mask_target = unsafe {
        std::slice::from_raw_parts_mut(working_mask_output, working_mask_output_length as usize)
    };
    mask_target.copy_from_slice(&working_mask);
    let node_target = unsafe { std::slice::from_raw_parts_mut(nodes_output, required_node_values) };
    for (row, node) in node_target.chunks_exact_mut(19).zip(&nodes) {
        row[0..4].copy_from_slice(&node.bbox.map(|value| value as u32));
        row[4] = node.depth as u32;
        row[5] = node.parent_index.map_or(0, |value| value as u32 + 1);
        row[6] = node.rows.map_or(0, |rows| if rows { 1 } else { 2 });
        row[7] = node.child_indexes[0].map_or(0, |value| value as u32 + 1);
        row[8] = node.child_indexes[1].map_or(0, |value| value as u32 + 1);
        if let Some(separator) = node.separator {
            row[9] = 1;
            row[10..14].copy_from_slice(&separator.map(|value| value as u32));
        }
        row[14] = node.split_coordinate.map_or(0, |value| value as u32 + 1);
        row[15] = match node.stop_reason {
            None => 0,
            Some(geometry::PartitionStopReason::Atomic) => 1,
            Some(geometry::PartitionStopReason::Empty) => 2,
            Some(geometry::PartitionStopReason::Limit) => 3,
        };
        row[16] = u32::from(node.rule_partition);
        row[17] = u32::from(node.row_rule_top);
        row[18] = u32::from(node.row_rule_bottom);
    }
    i64::try_from(nodes.len()).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_fragment_components_for_leaves_copy(
    components: *const u32,
    components_length: u32,
    runs: *const u32,
    runs_length: u32,
    nodes: *const u32,
    nodes_length: u32,
    component_output: *mut u32,
    component_output_length: u32,
    run_output: *mut u32,
    run_output_length: u32,
) -> i64 {
    if components_length % 5 != 0
        || runs_length % 4 != 0
        || nodes_length == 0
        || nodes_length % 19 != 0
        || component_output_length % 5 != 0
        || run_output_length % 4 != 0
        || nodes.is_null()
        || component_output.is_null()
        || run_output.is_null()
        || (components_length > 0 && components.is_null())
        || (runs_length > 0 && runs.is_null())
    {
        return -1;
    }
    let packed_components = if components_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(components, components_length as usize) }
    };
    let components: Vec<geometry::ConnectedComponent> = packed_components
        .chunks_exact(5)
        .map(|row| geometry::ConnectedComponent {
            bbox: [
                row[0] as usize,
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
            ],
            pixels: row[4] as usize,
        })
        .collect();
    let packed_runs = if runs_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(runs, runs_length as usize) }
    };
    let runs: Vec<geometry::ConnectedComponentRun> = packed_runs
        .chunks_exact(4)
        .map(|row| geometry::ConnectedComponentRun {
            component_index: row[0] as usize,
            row: row[1] as usize,
            start: row[2] as usize,
            stop: row[3] as usize,
        })
        .collect();
    let packed_nodes = unsafe { std::slice::from_raw_parts(nodes, nodes_length as usize) };
    let nodes: Vec<geometry::PartitionNode> = packed_nodes
        .chunks_exact(19)
        .map(|row| geometry::PartitionNode {
            bbox: [
                row[0] as usize,
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
            ],
            depth: row[4] as usize,
            parent_index: (row[5] > 0).then_some(row[5].saturating_sub(1) as usize),
            rows: match row[6] {
                1 => Some(true),
                2 => Some(false),
                _ => None,
            },
            child_indexes: [
                (row[7] > 0).then_some(row[7].saturating_sub(1) as usize),
                (row[8] > 0).then_some(row[8].saturating_sub(1) as usize),
            ],
            separator: (row[9] != 0).then_some([
                row[10] as usize,
                row[11] as usize,
                row[12] as usize,
                row[13] as usize,
            ]),
            split_coordinate: (row[14] > 0).then_some(row[14].saturating_sub(1) as usize),
            stop_reason: match row[15] {
                1 => Some(geometry::PartitionStopReason::Atomic),
                2 => Some(geometry::PartitionStopReason::Empty),
                3 => Some(geometry::PartitionStopReason::Limit),
                _ => None,
            },
            rule_partition: row[16] != 0,
            row_rule_top: row[17] != 0,
            row_rule_bottom: row[18] != 0,
        })
        .collect();
    let Some((fragments, fragment_runs)) =
        geometry::fragment_components_for_leaves(&components, &runs, &nodes)
    else {
        return -1;
    };
    if component_output_length as usize != fragments.len().saturating_mul(5)
        || run_output_length as usize != fragment_runs.len().saturating_mul(4)
    {
        return -2;
    }
    let component_target = unsafe {
        std::slice::from_raw_parts_mut(component_output, component_output_length as usize)
    };
    for (row, component) in component_target.chunks_exact_mut(5).zip(&fragments) {
        row[0..4].copy_from_slice(&component.bbox.map(|value| value as u32));
        row[4] = component.pixels as u32;
    }
    let run_target =
        unsafe { std::slice::from_raw_parts_mut(run_output, run_output_length as usize) };
    for (row, run) in run_target.chunks_exact_mut(4).zip(&fragment_runs) {
        row[0] = run.component_index as u32;
        row[1] = run.row as u32;
        row[2] = run.start as u32;
        row[3] = run.stop as u32;
    }
    i64::try_from(fragments.len()).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_thin_line_axis(
    left: u32,
    top: u32,
    right: u32,
    bottom: u32,
    pixels: u32,
    structural_bands: *const u32,
    structural_bands_length: u32,
    maximum_rule_thickness: u32,
    minimum_rule_length: u32,
) -> i32 {
    if structural_bands_length % 5 != 0
        || (structural_bands_length > 0 && structural_bands.is_null())
    {
        return -1;
    }
    let packed = if structural_bands_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(structural_bands, structural_bands_length as usize) }
    };
    let bands: Vec<geometry::RuleDraftSummary> = packed
        .chunks_exact(5)
        .map(|row| geometry::RuleDraftSummary {
            horizontal: row[0] == 0,
            bbox: [
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
                row[4] as usize,
            ],
        })
        .collect();
    match geometry::thin_line_axis(
        [left as usize, top as usize, right as usize, bottom as usize],
        pixels as usize,
        &bands,
        maximum_rule_thickness as usize,
        minimum_rule_length as usize,
    ) {
        Some(Some(true)) => 1,
        Some(Some(false)) => 2,
        Some(None) => 0,
        None => -1,
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_unsupported_horizontal_line_component(
    component_index: u32,
    non_rule_foreground: *const u8,
    non_rule_foreground_length: u32,
    width: u32,
    height: u32,
    components: *const u32,
    components_length: u32,
    body_height: f64,
    maximum_rule_thickness: u32,
    minimum_rule_length: u32,
    minimum_rule_aspect_ratio: f64,
) -> i32 {
    if non_rule_foreground.is_null()
        || non_rule_foreground_length == 0
        || components_length == 0
        || components_length % 5 != 0
        || components.is_null()
    {
        return -1;
    }
    let mask = unsafe {
        std::slice::from_raw_parts(non_rule_foreground, non_rule_foreground_length as usize)
    };
    let packed = unsafe { std::slice::from_raw_parts(components, components_length as usize) };
    let components: Vec<geometry::ConnectedComponent> = packed
        .chunks_exact(5)
        .map(|row| geometry::ConnectedComponent {
            bbox: [
                row[0] as usize,
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
            ],
            pixels: row[4] as usize,
        })
        .collect();
    match geometry::unsupported_horizontal_line_component(
        component_index as usize,
        mask,
        width as usize,
        height as usize,
        &components,
        body_height,
        maximum_rule_thickness as usize,
        minimum_rule_length as usize,
        minimum_rule_aspect_ratio,
    ) {
        Some(value) => i32::from(value),
        None => -1,
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_component_has_rule_evidence(
    left: u32,
    top: u32,
    right: u32,
    bottom: u32,
    pixels: u32,
    line_evidence: *const u8,
    line_evidence_length: u32,
    width: u32,
    height: u32,
) -> i32 {
    if line_evidence.is_null() || line_evidence_length == 0 {
        return -1;
    }
    let evidence =
        unsafe { std::slice::from_raw_parts(line_evidence, line_evidence_length as usize) };
    match geometry::component_has_rule_evidence(
        geometry::ConnectedComponent {
            bbox: [left as usize, top as usize, right as usize, bottom as usize],
            pixels: pixels as usize,
        },
        evidence,
        width as usize,
        height as usize,
    ) {
        Some(value) => i32::from(value),
        None => -1,
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_pure_vertical_line_component(
    left: u32,
    top: u32,
    right: u32,
    bottom: u32,
    pixels: u32,
) -> i32 {
    match geometry::pure_vertical_line_component(geometry::ConnectedComponent {
        bbox: [left as usize, top as usize, right as usize, bottom as usize],
        pixels: pixels as usize,
    }) {
        Some(value) => i32::from(value),
        None => -1,
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_horizontal_network_supported(
    left: u32,
    top: u32,
    right: u32,
    bottom: u32,
    pixels: u32,
    structural_bands: *const u32,
    structural_bands_length: u32,
    maximum_rule_thickness: u32,
) -> i32 {
    if structural_bands_length % 5 != 0
        || (structural_bands_length > 0 && structural_bands.is_null())
    {
        return -1;
    }
    let packed = if structural_bands_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(structural_bands, structural_bands_length as usize) }
    };
    let bands: Vec<geometry::RuleDraftSummary> = packed
        .chunks_exact(5)
        .map(|row| geometry::RuleDraftSummary {
            horizontal: row[0] == 0,
            bbox: [
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
                row[4] as usize,
            ],
        })
        .collect();
    match geometry::horizontal_network_supported(
        geometry::ConnectedComponent {
            bbox: [left as usize, top as usize, right as usize, bottom as usize],
            pixels: pixels as usize,
        },
        &bands,
        maximum_rule_thickness as usize,
    ) {
        Some(value) => i32::from(value),
        None => -1,
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_vertical_network_supported(
    component_index: u32,
    components: *const u32,
    components_length: u32,
    structural_bands: *const u32,
    structural_bands_length: u32,
    maximum_rule_thickness: u32,
    minimum_rule_length: u32,
) -> i32 {
    if components_length == 0
        || components_length % 5 != 0
        || structural_bands_length % 5 != 0
        || components.is_null()
        || (structural_bands_length > 0 && structural_bands.is_null())
    {
        return -1;
    }
    let packed_components =
        unsafe { std::slice::from_raw_parts(components, components_length as usize) };
    let components: Vec<geometry::ConnectedComponent> = packed_components
        .chunks_exact(5)
        .map(|row| geometry::ConnectedComponent {
            bbox: [
                row[0] as usize,
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
            ],
            pixels: row[4] as usize,
        })
        .collect();
    let packed_bands = if structural_bands_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(structural_bands, structural_bands_length as usize) }
    };
    let bands: Vec<geometry::RuleDraftSummary> = packed_bands
        .chunks_exact(5)
        .map(|row| geometry::RuleDraftSummary {
            horizontal: row[0] == 0,
            bbox: [
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
                row[4] as usize,
            ],
        })
        .collect();
    match geometry::vertical_network_supported(
        component_index as usize,
        &components,
        &bands,
        maximum_rule_thickness as usize,
        minimum_rule_length as usize,
    ) {
        Some(value) => i32::from(value),
        None => -1,
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_recover_residual_line_drafts_copy(
    non_rule_foreground: *const u8,
    non_rule_foreground_length: u32,
    line_evidence: *const u8,
    line_evidence_length: u32,
    width: u32,
    height: u32,
    rules: *const u32,
    rules_length: u32,
    components: *const u32,
    components_length: u32,
    maximum_rule_thickness: u32,
    minimum_rule_length: u32,
    minimum_rule_aspect_ratio: f64,
    output: *mut u32,
    output_length: u32,
) -> i64 {
    if non_rule_foreground.is_null()
        || line_evidence.is_null()
        || output.is_null()
        || non_rule_foreground_length == 0
        || line_evidence_length == 0
        || rules_length % 5 != 0
        || components_length % 5 != 0
        || output_length % 7 != 0
        || (rules_length > 0 && rules.is_null())
        || (components_length > 0 && components.is_null())
    {
        return -1;
    }
    let foreground = unsafe {
        std::slice::from_raw_parts(non_rule_foreground, non_rule_foreground_length as usize)
    };
    let evidence =
        unsafe { std::slice::from_raw_parts(line_evidence, line_evidence_length as usize) };
    let packed_rules = if rules_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(rules, rules_length as usize) }
    };
    let rules: Vec<geometry::RuleDraftSummary> = packed_rules
        .chunks_exact(5)
        .map(|row| geometry::RuleDraftSummary {
            horizontal: row[0] == 0,
            bbox: [
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
                row[4] as usize,
            ],
        })
        .collect();
    let packed_components = if components_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(components, components_length as usize) }
    };
    let components: Vec<geometry::ConnectedComponent> = packed_components
        .chunks_exact(5)
        .map(|row| geometry::ConnectedComponent {
            bbox: [
                row[0] as usize,
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
            ],
            pixels: row[4] as usize,
        })
        .collect();
    let Some(recovered) = geometry::recover_residual_line_drafts(
        foreground,
        evidence,
        width as usize,
        height as usize,
        &rules,
        &components,
        maximum_rule_thickness as usize,
        minimum_rule_length as usize,
        minimum_rule_aspect_ratio,
    ) else {
        return -1;
    };
    if output_length as usize != recovered.len().saturating_mul(7) {
        return -2;
    }
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    for (row, draft) in target.chunks_exact_mut(7).zip(&recovered) {
        row[0] = u32::from(!draft.summary.horizontal);
        row[1..5].copy_from_slice(&draft.summary.bbox.map(|value| value as u32));
        row[5] = u32::from(draft.claim_full_bbox);
        row[6] = u32::from(draft.partition_evidence);
    }
    i64::try_from(recovered.len()).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_recover_leaf_rule_networks_copy(
    rule_evidence: *const u8,
    rule_evidence_length: u32,
    unclaimed_foreground: *const u8,
    unclaimed_foreground_length: u32,
    claimed_rules: *const u8,
    claimed_rules_length: u32,
    width: u32,
    height: u32,
    rules: *const u32,
    rules_length: u32,
    nodes: *const u32,
    nodes_length: u32,
    components: *const u32,
    components_length: u32,
    runs: *const u32,
    runs_length: u32,
    maximum_rule_thickness: u32,
    minimum_rule_length: u32,
    minimum_rule_aspect_ratio: f64,
    output: *mut u32,
    output_length: u32,
) -> i64 {
    if rule_evidence.is_null()
        || unclaimed_foreground.is_null()
        || claimed_rules.is_null()
        || nodes.is_null()
        || output.is_null()
        || rule_evidence_length == 0
        || unclaimed_foreground_length == 0
        || claimed_rules_length == 0
        || rules_length % 5 != 0
        || nodes_length == 0
        || nodes_length % 19 != 0
        || components_length % 5 != 0
        || runs_length % 4 != 0
        || output_length % 7 != 0
        || (rules_length > 0 && rules.is_null())
        || (components_length > 0 && components.is_null())
        || (runs_length > 0 && runs.is_null())
    {
        return -1;
    }
    let evidence =
        unsafe { std::slice::from_raw_parts(rule_evidence, rule_evidence_length as usize) };
    let unclaimed = unsafe {
        std::slice::from_raw_parts(unclaimed_foreground, unclaimed_foreground_length as usize)
    };
    let claimed =
        unsafe { std::slice::from_raw_parts(claimed_rules, claimed_rules_length as usize) };
    let packed_rules = if rules_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(rules, rules_length as usize) }
    };
    let rules: Vec<geometry::RuleDraftSummary> = packed_rules
        .chunks_exact(5)
        .map(|row| geometry::RuleDraftSummary {
            horizontal: row[0] == 0,
            bbox: [
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
                row[4] as usize,
            ],
        })
        .collect();
    let packed_nodes = unsafe { std::slice::from_raw_parts(nodes, nodes_length as usize) };
    let nodes: Vec<geometry::PartitionNode> = packed_nodes
        .chunks_exact(19)
        .map(|row| geometry::PartitionNode {
            bbox: [
                row[0] as usize,
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
            ],
            depth: row[4] as usize,
            parent_index: (row[5] > 0).then_some(row[5].saturating_sub(1) as usize),
            rows: match row[6] {
                1 => Some(true),
                2 => Some(false),
                _ => None,
            },
            child_indexes: [
                (row[7] > 0).then_some(row[7].saturating_sub(1) as usize),
                (row[8] > 0).then_some(row[8].saturating_sub(1) as usize),
            ],
            separator: (row[9] != 0).then_some([
                row[10] as usize,
                row[11] as usize,
                row[12] as usize,
                row[13] as usize,
            ]),
            split_coordinate: (row[14] > 0).then_some(row[14].saturating_sub(1) as usize),
            stop_reason: match row[15] {
                1 => Some(geometry::PartitionStopReason::Atomic),
                2 => Some(geometry::PartitionStopReason::Empty),
                3 => Some(geometry::PartitionStopReason::Limit),
                _ => None,
            },
            rule_partition: row[16] != 0,
            row_rule_top: row[17] != 0,
            row_rule_bottom: row[18] != 0,
        })
        .collect();
    let packed_components = if components_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(components, components_length as usize) }
    };
    let components: Vec<geometry::ConnectedComponent> = packed_components
        .chunks_exact(5)
        .map(|row| geometry::ConnectedComponent {
            bbox: [
                row[0] as usize,
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
            ],
            pixels: row[4] as usize,
        })
        .collect();
    let packed_runs = if runs_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(runs, runs_length as usize) }
    };
    let runs: Vec<geometry::ConnectedComponentRun> = packed_runs
        .chunks_exact(4)
        .map(|row| geometry::ConnectedComponentRun {
            component_index: row[0] as usize,
            row: row[1] as usize,
            start: row[2] as usize,
            stop: row[3] as usize,
        })
        .collect();
    let Some(recovered) = geometry::recover_leaf_rule_networks(
        evidence,
        unclaimed,
        claimed,
        width as usize,
        height as usize,
        &rules,
        &nodes,
        &components,
        &runs,
        maximum_rule_thickness as usize,
        minimum_rule_length as usize,
        minimum_rule_aspect_ratio,
    ) else {
        return -1;
    };
    if output_length as usize != recovered.len().saturating_mul(7) {
        return -2;
    }
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    for (row, draft) in target.chunks_exact_mut(7).zip(&recovered) {
        row[0] = u32::from(!draft.summary.horizontal);
        row[1..5].copy_from_slice(&draft.summary.bbox.map(|value| value as u32));
        row[5] = u32::from(draft.claim_full_bbox);
        row[6] = u32::from(draft.partition_evidence);
    }
    i64::try_from(recovered.len()).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_best_component_boundary_copy(
    components: *const u32,
    components_length: u32,
    runs: *const u32,
    runs_length: u32,
    left: u32,
    top: u32,
    right: u32,
    bottom: u32,
    diacritic_guard: *const u8,
    diacritic_guard_length: u32,
    width: u32,
    height: u32,
    membership_output: *mut u8,
    membership_output_length: u32,
) -> i64 {
    if components_length % 6 != 0
        || runs_length % 4 != 0
        || diacritic_guard.is_null()
        || membership_output.is_null()
        || (components_length > 0 && components.is_null())
        || (runs_length > 0 && runs.is_null())
    {
        return -1;
    }
    let packed_components = if components_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(components, components_length as usize) }
    };
    let component_ids: Vec<usize> = packed_components
        .chunks_exact(6)
        .map(|row| row[0] as usize)
        .collect();
    let components: Vec<geometry::ConnectedComponent> = packed_components
        .chunks_exact(6)
        .map(|row| geometry::ConnectedComponent {
            bbox: [
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
                row[4] as usize,
            ],
            pixels: row[5] as usize,
        })
        .collect();
    let packed_runs = if runs_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(runs, runs_length as usize) }
    };
    let runs: Vec<geometry::ConnectedComponentRun> = packed_runs
        .chunks_exact(4)
        .map(|row| geometry::ConnectedComponentRun {
            component_index: row[0] as usize,
            row: row[1] as usize,
            start: row[2] as usize,
            stop: row[3] as usize,
        })
        .collect();
    let guard =
        unsafe { std::slice::from_raw_parts(diacritic_guard, diacritic_guard_length as usize) };
    let Some(decision) = geometry::best_component_boundary(
        &components,
        &component_ids,
        &runs,
        [left as usize, top as usize, right as usize, bottom as usize],
        guard,
        width as usize,
        height as usize,
    ) else {
        return -1;
    };
    if membership_output_length as usize != components.len() {
        return -1;
    }
    let membership = unsafe {
        std::slice::from_raw_parts_mut(membership_output, membership_output_length as usize)
    };
    membership.fill(0);
    let Some(decision) = decision else {
        return -2;
    };
    for index in decision.upper_indexes {
        membership[index] = 1;
    }
    for index in decision.lower_indexes {
        membership[index] = 2;
    }
    i64::try_from(decision.coordinate).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_best_component_rule_boundary_copy(
    components: *const u32,
    components_length: u32,
    left: u32,
    top: u32,
    right: u32,
    bottom: u32,
    rules: *const u32,
    rules_length: u32,
    separator_output: *mut u32,
    separator_output_length: u32,
    membership_output: *mut u8,
    membership_output_length: u32,
) -> i64 {
    if components_length % 6 != 0
        || rules_length % 5 != 0
        || separator_output.is_null()
        || membership_output.is_null()
        || separator_output_length != 4
        || membership_output_length as usize != components_length as usize / 6
        || (components_length > 0 && components.is_null())
        || (rules_length > 0 && rules.is_null())
    {
        return -1;
    }
    let packed_components = if components_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(components, components_length as usize) }
    };
    let components: Vec<geometry::ConnectedComponent> = packed_components
        .chunks_exact(6)
        .map(|row| geometry::ConnectedComponent {
            bbox: [
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
                row[4] as usize,
            ],
            pixels: row[5] as usize,
        })
        .collect();
    let packed_rules = if rules_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(rules, rules_length as usize) }
    };
    let rules: Vec<geometry::RuleDraftSummary> = packed_rules
        .chunks_exact(5)
        .map(|row| geometry::RuleDraftSummary {
            horizontal: row[0] == 0,
            bbox: [
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
                row[4] as usize,
            ],
        })
        .collect();
    let separator = unsafe {
        std::slice::from_raw_parts_mut(separator_output, separator_output_length as usize)
    };
    let membership = unsafe {
        std::slice::from_raw_parts_mut(membership_output, membership_output_length as usize)
    };
    separator.fill(0);
    membership.fill(0);
    let Some(decision) = geometry::best_component_rule_boundary(
        &components,
        [left as usize, top as usize, right as usize, bottom as usize],
        &rules,
    ) else {
        return -1;
    };
    let Some(decision) = decision else {
        return -2;
    };
    separator.copy_from_slice(&decision.separator.map(|value| value as u32));
    for index in decision.left_indexes {
        membership[index] = 1;
    }
    for index in decision.right_indexes {
        membership[index] = 2;
    }
    1
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_refine_component_boundaries_copy(
    packed_nodes: *const u32,
    packed_nodes_length: u32,
    packed_components: *const u32,
    packed_components_length: u32,
    packed_runs: *const u32,
    packed_runs_length: u32,
    packed_rules: *const u32,
    packed_rules_length: u32,
    diacritic_guard: *const u8,
    diacritic_guard_length: u32,
    width: u32,
    height: u32,
    max_nodes: u32,
    max_depth: u32,
    min_safe_gap: u32,
    output: *mut u32,
    output_length: u32,
) -> i64 {
    if packed_nodes_length % 19 != 0
        || packed_components_length % 6 != 0
        || packed_runs_length % 4 != 0
        || packed_rules_length % 5 != 0
        || output_length % 19 != 0
        || diacritic_guard.is_null()
        || output.is_null()
        || (packed_nodes_length > 0 && packed_nodes.is_null())
        || (packed_components_length > 0 && packed_components.is_null())
        || (packed_runs_length > 0 && packed_runs.is_null())
        || (packed_rules_length > 0 && packed_rules.is_null())
    {
        return -1;
    }
    let packed_nodes = if packed_nodes_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(packed_nodes, packed_nodes_length as usize) }
    };
    let nodes: Option<Vec<geometry::PartitionNode>> = packed_nodes
        .chunks_exact(19)
        .map(|row| {
            let rows = match row[6] {
                0 => None,
                1 => Some(true),
                2 => Some(false),
                _ => return None,
            };
            let stop_reason = match row[15] {
                0 => None,
                1 => Some(geometry::PartitionStopReason::Atomic),
                2 => Some(geometry::PartitionStopReason::Empty),
                3 => Some(geometry::PartitionStopReason::Limit),
                _ => return None,
            };
            Some(geometry::PartitionNode {
                bbox: [
                    row[0] as usize,
                    row[1] as usize,
                    row[2] as usize,
                    row[3] as usize,
                ],
                depth: row[4] as usize,
                parent_index: (row[5] > 0).then_some(row[5].saturating_sub(1) as usize),
                rows,
                child_indexes: [
                    (row[7] > 0).then_some(row[7].saturating_sub(1) as usize),
                    (row[8] > 0).then_some(row[8].saturating_sub(1) as usize),
                ],
                separator: (row[9] != 0).then_some([
                    row[10] as usize,
                    row[11] as usize,
                    row[12] as usize,
                    row[13] as usize,
                ]),
                split_coordinate: (row[14] > 0).then_some(row[14].saturating_sub(1) as usize),
                stop_reason,
                rule_partition: row[16] != 0,
                row_rule_top: row[17] != 0,
                row_rule_bottom: row[18] != 0,
            })
        })
        .collect();
    let Some(nodes) = nodes else {
        return -1;
    };
    let packed_components = if packed_components_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(packed_components, packed_components_length as usize) }
    };
    let component_ids: Vec<usize> = packed_components
        .chunks_exact(6)
        .map(|row| row[0] as usize)
        .collect();
    let components: Vec<geometry::ConnectedComponent> = packed_components
        .chunks_exact(6)
        .map(|row| geometry::ConnectedComponent {
            bbox: [
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
                row[4] as usize,
            ],
            pixels: row[5] as usize,
        })
        .collect();
    let packed_runs = if packed_runs_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(packed_runs, packed_runs_length as usize) }
    };
    let runs: Vec<geometry::ConnectedComponentRun> = packed_runs
        .chunks_exact(4)
        .map(|row| geometry::ConnectedComponentRun {
            component_index: row[0] as usize,
            row: row[1] as usize,
            start: row[2] as usize,
            stop: row[3] as usize,
        })
        .collect();
    let packed_rules = if packed_rules_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(packed_rules, packed_rules_length as usize) }
    };
    let rules: Vec<geometry::RuleDraftSummary> = packed_rules
        .chunks_exact(5)
        .map(|row| geometry::RuleDraftSummary {
            horizontal: row[0] == 0,
            bbox: [
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
                row[4] as usize,
            ],
        })
        .collect();
    let guard =
        unsafe { std::slice::from_raw_parts(diacritic_guard, diacritic_guard_length as usize) };
    let Some(refined) = geometry::refine_component_boundaries(
        &nodes,
        &components,
        &component_ids,
        &runs,
        guard,
        width as usize,
        height as usize,
        geometry::PartitionConfig {
            max_nodes: max_nodes as usize,
            max_depth: max_depth as usize,
            min_safe_gap: min_safe_gap as usize,
        },
        &rules,
    ) else {
        return -1;
    };
    if refined.len().saturating_mul(19) > output_length as usize {
        return -2;
    }
    let output = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    output.fill(0);
    for (row, node) in output.chunks_exact_mut(19).zip(&refined) {
        row[0..4].copy_from_slice(&node.bbox.map(|value| value as u32));
        row[4] = node.depth as u32;
        row[5] = node.parent_index.map_or(0, |index| index as u32 + 1);
        row[6] = node.rows.map_or(0, |rows| if rows { 1 } else { 2 });
        row[7] = node.child_indexes[0].map_or(0, |index| index as u32 + 1);
        row[8] = node.child_indexes[1].map_or(0, |index| index as u32 + 1);
        if let Some(separator) = node.separator {
            row[9] = 1;
            row[10..14].copy_from_slice(&separator.map(|value| value as u32));
        }
        row[14] = node
            .split_coordinate
            .map_or(0, |coordinate| coordinate as u32 + 1);
        row[15] = match node.stop_reason {
            None => 0,
            Some(geometry::PartitionStopReason::Atomic) => 1,
            Some(geometry::PartitionStopReason::Empty) => 2,
            Some(geometry::PartitionStopReason::Limit) => 3,
        };
        row[16] = u32::from(node.rule_partition);
        row[17] = u32::from(node.row_rule_top);
        row[18] = u32::from(node.row_rule_bottom);
    }
    i64::try_from(refined.len()).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_materialize_segments_copy(
    packed_components: *const u32,
    packed_components_length: u32,
    packed_runs: *const u32,
    packed_runs_length: u32,
    packed_nodes: *const u32,
    packed_nodes_length: u32,
    inverse: *const f64,
    inverse_length: u32,
    original_width: u32,
    original_height: u32,
    width: u32,
    height: u32,
    segment_output: *mut u32,
    segment_output_length: u32,
    component_ids_output: *mut u32,
    component_ids_output_length: u32,
    ownership_output: *mut i32,
    ownership_output_length: u32,
    leaf_output: *mut u32,
    leaf_output_length: u32,
) -> i64 {
    if packed_components_length % 6 != 0
        || packed_runs_length % 4 != 0
        || packed_nodes_length % 19 != 0
        || inverse_length != 9
        || segment_output_length % 14 != 0
        || component_ids_output_length as usize != packed_components_length as usize / 6
        || ownership_output_length as usize != (width as usize).saturating_mul(height as usize)
        || leaf_output_length as usize != packed_nodes_length as usize / 19
        || inverse.is_null()
        || segment_output.is_null()
        || component_ids_output.is_null()
        || ownership_output.is_null()
        || leaf_output.is_null()
        || (packed_components_length > 0 && packed_components.is_null())
        || (packed_runs_length > 0 && packed_runs.is_null())
        || (packed_nodes_length > 0 && packed_nodes.is_null())
    {
        return -1;
    }
    let packed_components = if packed_components_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(packed_components, packed_components_length as usize) }
    };
    let component_ids: Vec<usize> = packed_components
        .chunks_exact(6)
        .map(|row| row[0] as usize)
        .collect();
    let components: Vec<geometry::ConnectedComponent> = packed_components
        .chunks_exact(6)
        .map(|row| geometry::ConnectedComponent {
            bbox: [
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
                row[4] as usize,
            ],
            pixels: row[5] as usize,
        })
        .collect();
    let packed_runs = if packed_runs_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(packed_runs, packed_runs_length as usize) }
    };
    let runs: Vec<geometry::ConnectedComponentRun> = packed_runs
        .chunks_exact(4)
        .map(|row| geometry::ConnectedComponentRun {
            component_index: row[0] as usize,
            row: row[1] as usize,
            start: row[2] as usize,
            stop: row[3] as usize,
        })
        .collect();
    let packed_nodes = if packed_nodes_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(packed_nodes, packed_nodes_length as usize) }
    };
    let nodes: Option<Vec<geometry::PartitionNode>> = packed_nodes
        .chunks_exact(19)
        .map(|row| {
            let rows = match row[6] {
                0 => None,
                1 => Some(true),
                2 => Some(false),
                _ => return None,
            };
            let stop_reason = match row[15] {
                0 => None,
                1 => Some(geometry::PartitionStopReason::Atomic),
                2 => Some(geometry::PartitionStopReason::Empty),
                3 => Some(geometry::PartitionStopReason::Limit),
                _ => return None,
            };
            Some(geometry::PartitionNode {
                bbox: [
                    row[0] as usize,
                    row[1] as usize,
                    row[2] as usize,
                    row[3] as usize,
                ],
                depth: row[4] as usize,
                parent_index: (row[5] > 0).then_some(row[5].saturating_sub(1) as usize),
                rows,
                child_indexes: [
                    (row[7] > 0).then_some(row[7].saturating_sub(1) as usize),
                    (row[8] > 0).then_some(row[8].saturating_sub(1) as usize),
                ],
                separator: (row[9] != 0).then_some([
                    row[10] as usize,
                    row[11] as usize,
                    row[12] as usize,
                    row[13] as usize,
                ]),
                split_coordinate: (row[14] > 0).then_some(row[14].saturating_sub(1) as usize),
                stop_reason,
                rule_partition: row[16] != 0,
                row_rule_top: row[17] != 0,
                row_rule_bottom: row[18] != 0,
            })
        })
        .collect();
    let Some(nodes) = nodes else {
        return -1;
    };
    let inverse_values = unsafe { std::slice::from_raw_parts(inverse, inverse_length as usize) };
    let mut inverse_matrix = [0.0_f64; 9];
    inverse_matrix.copy_from_slice(inverse_values);
    let Some(materialized) = geometry::materialize_segments(
        &components,
        &component_ids,
        &runs,
        &nodes,
        geometry::AffineBoxTransform {
            original_size: [original_width as usize, original_height as usize],
            inverse: inverse_matrix,
        },
        width as usize,
        height as usize,
    ) else {
        return -2;
    };
    if materialized.segments.len().saturating_mul(14) > segment_output_length as usize {
        return -3;
    }
    let segment_output =
        unsafe { std::slice::from_raw_parts_mut(segment_output, segment_output_length as usize) };
    let component_ids_output = unsafe {
        std::slice::from_raw_parts_mut(component_ids_output, component_ids_output_length as usize)
    };
    let ownership_output = unsafe {
        std::slice::from_raw_parts_mut(ownership_output, ownership_output_length as usize)
    };
    let leaf_output =
        unsafe { std::slice::from_raw_parts_mut(leaf_output, leaf_output_length as usize) };
    segment_output.fill(0);
    component_ids_output.fill(0);
    leaf_output.fill(0);
    ownership_output.copy_from_slice(
        &materialized
            .ownership
            .iter()
            .map(|owner| *owner as i32)
            .collect::<Vec<_>>(),
    );
    let mut component_offset = 0_usize;
    for (segment_index, segment) in materialized.segments.iter().enumerate() {
        let row = &mut segment_output[segment_index * 14..segment_index * 14 + 14];
        row[0] = segment_index as u32;
        row[1..5].copy_from_slice(&segment.bbox.map(|value| value as u32));
        row[5..9].copy_from_slice(&segment.source_bbox.map(|value| value as u32));
        row[9] = segment.ink_pixels as u32;
        row[10] = segment.row_index as u32;
        row[11] = segment.leaf_index as u32;
        row[12] = component_offset as u32;
        row[13] = segment.component_ids.len() as u32;
        let stop = component_offset + segment.component_ids.len();
        for (target, component_id) in component_ids_output[component_offset..stop]
            .iter_mut()
            .zip(&segment.component_ids)
        {
            *target = *component_id as u32;
        }
        component_offset = stop;
    }
    for (leaf, segment) in leaf_output
        .iter_mut()
        .zip(&materialized.leaf_segment_indexes)
    {
        *leaf = segment.map_or(0, |index| index as u32 + 1);
    }
    i64::try_from(materialized.segments.len()).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_materialize_nodes_copy(
    packed_nodes: *const u32,
    packed_nodes_length: u32,
    leaf_segments: *const u32,
    leaf_segments_length: u32,
    packed_segments: *const u32,
    packed_segments_length: u32,
    node_output: *mut u32,
    node_output_length: u32,
    segment_output: *mut u32,
    segment_output_length: u32,
) -> i64 {
    if packed_nodes_length % 19 != 0
        || packed_segments_length % 14 != 0
        || node_output_length % 3 != 0
        || leaf_segments_length as usize != packed_nodes_length as usize / 19
        || packed_nodes.is_null()
        || leaf_segments.is_null()
        || packed_segments.is_null()
        || node_output.is_null()
        || segment_output.is_null()
    {
        return -1;
    }
    let packed_nodes =
        unsafe { std::slice::from_raw_parts(packed_nodes, packed_nodes_length as usize) };
    let nodes: Option<Vec<geometry::PartitionNode>> = packed_nodes
        .chunks_exact(19)
        .map(|row| {
            let rows = match row[6] {
                0 => None,
                1 => Some(true),
                2 => Some(false),
                _ => return None,
            };
            let stop_reason = match row[15] {
                0 => None,
                1 => Some(geometry::PartitionStopReason::Atomic),
                2 => Some(geometry::PartitionStopReason::Empty),
                3 => Some(geometry::PartitionStopReason::Limit),
                _ => return None,
            };
            Some(geometry::PartitionNode {
                bbox: [
                    row[0] as usize,
                    row[1] as usize,
                    row[2] as usize,
                    row[3] as usize,
                ],
                depth: row[4] as usize,
                parent_index: (row[5] > 0).then_some(row[5].saturating_sub(1) as usize),
                rows,
                child_indexes: [
                    (row[7] > 0).then_some(row[7].saturating_sub(1) as usize),
                    (row[8] > 0).then_some(row[8].saturating_sub(1) as usize),
                ],
                separator: (row[9] != 0).then_some([
                    row[10] as usize,
                    row[11] as usize,
                    row[12] as usize,
                    row[13] as usize,
                ]),
                split_coordinate: (row[14] > 0).then_some(row[14].saturating_sub(1) as usize),
                stop_reason,
                rule_partition: row[16] != 0,
                row_rule_top: row[17] != 0,
                row_rule_bottom: row[18] != 0,
            })
        })
        .collect();
    let Some(nodes) = nodes else {
        return -1;
    };
    let leaf_segments =
        unsafe { std::slice::from_raw_parts(leaf_segments, leaf_segments_length as usize) };
    let leaf_segment_indexes: Vec<Option<usize>> = leaf_segments
        .iter()
        .map(|value| (*value > 0).then_some(value.saturating_sub(1) as usize))
        .collect();
    let packed_segments =
        unsafe { std::slice::from_raw_parts(packed_segments, packed_segments_length as usize) };
    let segments: Vec<geometry::MaterializedSegment> = packed_segments
        .chunks_exact(14)
        .map(|row| geometry::MaterializedSegment {
            bbox: [
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
                row[4] as usize,
            ],
            source_bbox: [
                row[5] as usize,
                row[6] as usize,
                row[7] as usize,
                row[8] as usize,
            ],
            ink_pixels: row[9] as usize,
            row_index: row[10] as usize,
            leaf_index: row[11] as usize,
            component_ids: Vec::new(),
        })
        .collect();
    let Some(materialized) = geometry::materialize_nodes(&nodes, &leaf_segment_indexes, &segments)
    else {
        return -2;
    };
    let required_segments: usize = materialized
        .iter()
        .map(|node| node.segment_indexes.len())
        .sum();
    if materialized.len().saturating_mul(3) > node_output_length as usize
        || required_segments > segment_output_length as usize
    {
        return -3;
    }
    let node_output =
        unsafe { std::slice::from_raw_parts_mut(node_output, node_output_length as usize) };
    let segment_output =
        unsafe { std::slice::from_raw_parts_mut(segment_output, segment_output_length as usize) };
    node_output.fill(0);
    segment_output.fill(0);
    let mut segment_offset = 0_usize;
    for (output_index, node) in materialized.iter().enumerate() {
        let row = &mut node_output[output_index * 3..output_index * 3 + 3];
        row[0] = node.source_index as u32;
        row[1] = segment_offset as u32;
        row[2] = node.segment_indexes.len() as u32;
        let stop = segment_offset + node.segment_indexes.len();
        for (target, segment_index) in segment_output[segment_offset..stop]
            .iter_mut()
            .zip(&node.segment_indexes)
        {
            *target = *segment_index as u32;
        }
        segment_offset = stop;
    }
    i64::try_from(materialized.len()).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_project_sparse_matrix_copy(
    width: u32,
    height: u32,
    ownership: *const i32,
    ownership_length: u32,
    segment_count: u32,
    packed_rules: *const u32,
    packed_rules_length: u32,
    row_output: *mut u32,
    row_output_length: u32,
    column_output: *mut u32,
    column_output_length: u32,
    cell_output: *mut u32,
    cell_output_length: u32,
    span_output: *mut u32,
    span_output_length: u32,
    horizontal_output: *mut u32,
    horizontal_output_length: u32,
    vertical_output: *mut u32,
    vertical_output_length: u32,
    counts_output: *mut u32,
    counts_output_length: u32,
) -> i64 {
    if ownership_length as usize != (width as usize).saturating_mul(height as usize)
        || packed_rules_length % 5 != 0
        || row_output_length % 3 != 0
        || column_output_length % 3 != 0
        || cell_output_length % 3 != 0
        || span_output_length % 5 != 0
        || counts_output_length != 6
        || ownership.is_null()
        || counts_output.is_null()
        || (packed_rules_length > 0 && packed_rules.is_null())
        || (row_output_length > 0 && row_output.is_null())
        || (column_output_length > 0 && column_output.is_null())
        || (cell_output_length > 0 && cell_output.is_null())
        || (span_output_length > 0 && span_output.is_null())
        || (horizontal_output_length > 0 && horizontal_output.is_null())
        || (vertical_output_length > 0 && vertical_output.is_null())
    {
        return -1;
    }
    let ownership = unsafe { std::slice::from_raw_parts(ownership, ownership_length as usize) };
    let ownership: Vec<isize> = ownership.iter().map(|owner| *owner as isize).collect();
    let packed_rules = if packed_rules_length == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(packed_rules, packed_rules_length as usize) }
    };
    let rules: Vec<geometry::RuleDraftSummary> = packed_rules
        .chunks_exact(5)
        .map(|row| geometry::RuleDraftSummary {
            horizontal: row[0] == 0,
            bbox: [
                row[1] as usize,
                row[2] as usize,
                row[3] as usize,
                row[4] as usize,
            ],
        })
        .collect();
    let Some(matrix) = geometry::project_sparse_matrix(
        width as usize,
        height as usize,
        &ownership,
        segment_count as usize,
        &rules,
    ) else {
        return -2;
    };
    if matrix.rows.len().saturating_mul(3) > row_output_length as usize
        || matrix.columns.len().saturating_mul(3) > column_output_length as usize
        || matrix.cells.len().saturating_mul(3) > cell_output_length as usize
        || matrix.spans.len().saturating_mul(5) > span_output_length as usize
        || matrix.horizontal_rule_rows.len() > horizontal_output_length as usize
        || matrix.vertical_rule_columns.len() > vertical_output_length as usize
    {
        return -3;
    }
    let mutable_slice = |pointer: *mut u32, length: u32| {
        if length == 0 {
            &mut []
        } else {
            unsafe { std::slice::from_raw_parts_mut(pointer, length as usize) }
        }
    };
    let row_output = mutable_slice(row_output, row_output_length);
    let column_output = mutable_slice(column_output, column_output_length);
    let cell_output = mutable_slice(cell_output, cell_output_length);
    let span_output = mutable_slice(span_output, span_output_length);
    let horizontal_output = mutable_slice(horizontal_output, horizontal_output_length);
    let vertical_output = mutable_slice(vertical_output, vertical_output_length);
    let counts_output = mutable_slice(counts_output, counts_output_length);
    row_output.fill(0);
    column_output.fill(0);
    cell_output.fill(0);
    span_output.fill(0);
    horizontal_output.fill(0);
    vertical_output.fill(0);
    for (index, interval) in matrix.rows.iter().enumerate() {
        row_output[index * 3..index * 3 + 3].copy_from_slice(&[
            index as u32,
            interval.start as u32,
            interval.end as u32,
        ]);
    }
    for (index, interval) in matrix.columns.iter().enumerate() {
        column_output[index * 3..index * 3 + 3].copy_from_slice(&[
            index as u32,
            interval.start as u32,
            interval.end as u32,
        ]);
    }
    for (index, cell) in matrix.cells.iter().enumerate() {
        cell_output[index * 3..index * 3 + 3].copy_from_slice(&[
            cell.row as u32,
            cell.column as u32,
            cell.segment_index as u32,
        ]);
    }
    for (index, span) in matrix.spans.iter().enumerate() {
        span_output[index * 5..index * 5 + 5].copy_from_slice(&[
            span.segment_index as u32,
            span.row_start as u32,
            span.row_stop as u32,
            span.column_start as u32,
            span.column_stop as u32,
        ]);
    }
    for (target, value) in horizontal_output
        .iter_mut()
        .zip(&matrix.horizontal_rule_rows)
    {
        *target = *value as u32;
    }
    for (target, value) in vertical_output
        .iter_mut()
        .zip(&matrix.vertical_rule_columns)
    {
        *target = *value as u32;
    }
    counts_output.copy_from_slice(&[
        matrix.rows.len() as u32,
        matrix.columns.len() as u32,
        matrix.cells.len() as u32,
        matrix.spans.len() as u32,
        matrix.horizontal_rule_rows.len() as u32,
        matrix.vertical_rule_columns.len() as u32,
    ]);
    1
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_select_layout_foreground_copy(
    rgb: *const u8,
    rgb_length: u32,
    physical: *const u8,
    physical_length: u32,
    width: u32,
    height: u32,
    force_adaptive: u32,
    output: *mut u8,
    output_length: u32,
) -> i64 {
    if rgb.is_null()
        || physical.is_null()
        || output.is_null()
        || rgb_length == 0
        || physical_length == 0
    {
        return -1;
    }
    let rgb = unsafe { std::slice::from_raw_parts(rgb, rgb_length as usize) };
    let physical = unsafe { std::slice::from_raw_parts(physical, physical_length as usize) };
    let Some((selected, _mode)) = geometry::select_layout_foreground_mask(
        rgb,
        width as usize,
        height as usize,
        physical,
        force_adaptive != 0,
    ) else {
        return -1;
    };
    if output_length as usize != selected.len() {
        return -1;
    }
    let target = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    target.copy_from_slice(&selected);
    i64::try_from(selected.iter().filter(|value| **value != 0).count()).unwrap_or(i64::MAX)
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_pipeline_recipe_mask(capability_bits: u32) -> u32 {
    recipe_mask(PipelineCapabilities::from_bits(capability_bits))
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_sparse_add_signal(code: u32, signal: u32) -> i32 {
    let Ok(code) = u8::try_from(code) else {
        return -1;
    };
    let Ok(signal) = u8::try_from(signal) else {
        return -2;
    };
    match add_sparse_signal(code, signal) {
        Ok(value) => i32::from(value),
        Err(SparseCodeError::UnknownCode(_)) => -1,
        Err(SparseCodeError::UnknownSignal(_)) => -2,
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_is_isolated_heading(
    run_rows: u32,
    content_chars: u32,
    max_line_chars: u32,
    boundary_bits: u32,
) -> u32 {
    u32::from(is_isolated_heading(
        run_rows,
        content_chars,
        max_line_chars,
        boundary_bits & 1 != 0,
        boundary_bits & 2 != 0,
    ))
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_should_replace_primary(
    primary_chars: u32,
    fallback_chars: u32,
    primary_tokens: u32,
    retained_primary_tokens: u32,
) -> u32 {
    u32::from(should_replace_primary(
        primary_chars,
        fallback_chars,
        primary_tokens,
        retained_primary_tokens,
    ))
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_should_drop_text_block(
    candidate_chars: u32,
    existing_chars: u32,
    shared_tokens: u32,
    candidate_tokens: u32,
    existing_tokens: u32,
    similarity_milli: u32,
) -> u32 {
    u32::from(should_drop_text_block(
        candidate_chars,
        existing_chars,
        shared_tokens,
        candidate_tokens,
        existing_tokens,
        similarity_milli,
    ))
}

#[unsafe(no_mangle)]
pub extern "C" fn ittm_span_evidence_score(
    ocr_confidence_milli: u32,
    script_consistency_milli: u32,
    context_consistency_milli: u32,
    source_agreement: u32,
    contradictions: u32,
) -> i32 {
    score_span_evidence(SpanEvidence {
        ocr_confidence_milli,
        script_consistency_milli,
        context_consistency_milli,
        source_agreement,
        contradictions,
    })
}

fn geometry_fnv_bytes(values: &[u8]) -> u64 {
    values
        .iter()
        .fold(14_695_981_039_346_656_037_u64, |hash, value| {
            (hash ^ u64::from(*value)).wrapping_mul(1_099_511_628_211)
        })
}

fn geometry_fnv_values(values: impl IntoIterator<Item = u64>) -> u64 {
    let mut hash = 14_695_981_039_346_656_037_u64;
    for value in values {
        for byte in value.to_le_bytes() {
            hash = (hash ^ u64::from(byte)).wrapping_mul(1_099_511_628_211);
        }
    }
    hash
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_analyze_geometry_fingerprint_copy(
    pixels: *const u8,
    pixel_length: u32,
    width: u32,
    height: u32,
    stride: u32,
    channels: u32,
    output: *mut u64,
    output_length: u32,
) -> i32 {
    if pixels.is_null() || output.is_null() || output_length != 20 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(pixels, pixel_length as usize) };
    let Some(analysis) = geometry::analyze_geometry(
        source,
        width as usize,
        height as usize,
        stride as usize,
        channels as usize,
    ) else {
        return -2;
    };
    let mut source_to_preorder = vec![0_usize; analysis.partition_nodes.len()];
    for (preorder, node) in analysis.materialized_nodes.iter().enumerate() {
        source_to_preorder[node.source_index] = preorder;
    }
    let rule_values = analysis.rules.iter().flat_map(|rule| {
        [
            u64::from(!rule.summary.horizontal),
            rule.summary.bbox[0] as u64,
            rule.summary.bbox[1] as u64,
            rule.summary.bbox[2] as u64,
            rule.summary.bbox[3] as u64,
            rule.foreground_pixels as u64,
            (rule.strength * 1_000_000.0).round_ties_even() as u64,
        ]
    });
    let segment_values = analysis
        .materialized_segments
        .segments
        .iter()
        .flat_map(|segment| {
            [
                segment.bbox[0] as u64,
                segment.bbox[1] as u64,
                segment.bbox[2] as u64,
                segment.bbox[3] as u64,
                segment.ink_pixels as u64,
                segment.row_index as u64,
                source_to_preorder[segment.leaf_index] as u64,
            ]
        });
    let mut node_values = Vec::<u64>::new();
    for materialized in &analysis.materialized_nodes {
        let node = &analysis.partition_nodes[materialized.source_index];
        node_values.extend(node.bbox.map(|value| value as u64));
        node_values.push(node.depth as u64);
        node_values.push(
            node.parent_index
                .map_or(0, |index| source_to_preorder[index] as u64 + 1),
        );
        node_values.push(node.rows.map_or(0, |rows| if rows { 1 } else { 2 }));
        node_values
            .push(node.child_indexes[0].map_or(0, |index| source_to_preorder[index] as u64 + 1));
        node_values
            .push(node.child_indexes[1].map_or(0, |index| source_to_preorder[index] as u64 + 1));
        node_values.push(u64::from(node.separator.is_some()));
        node_values.extend(node.separator.unwrap_or([0; 4]).map(|value| value as u64));
        node_values.push(node.split_coordinate.map_or(0, |value| value as u64 + 1));
        node_values.push(match node.stop_reason {
            None => 0,
            Some(geometry::PartitionStopReason::Atomic) => 1,
            Some(geometry::PartitionStopReason::Empty) => 2,
            Some(geometry::PartitionStopReason::Limit) => 3,
        });
        node_values.push(materialized.segment_indexes.len() as u64);
        node_values.extend(
            materialized
                .segment_indexes
                .iter()
                .map(|index| *index as u64),
        );
    }
    let matrix = &analysis.sparse_matrix;
    let mut matrix_values = vec![
        matrix.rows.len() as u64,
        matrix.columns.len() as u64,
        matrix.cells.len() as u64,
        matrix.spans.len() as u64,
        matrix.horizontal_rule_rows.len() as u64,
        matrix.vertical_rule_columns.len() as u64,
    ];
    matrix_values.extend(
        matrix
            .rows
            .iter()
            .flat_map(|value| [value.start as u64, value.end as u64]),
    );
    matrix_values.extend(
        matrix
            .columns
            .iter()
            .flat_map(|value| [value.start as u64, value.end as u64]),
    );
    matrix_values.extend(matrix.cells.iter().flat_map(|value| {
        [
            value.row as u64,
            value.column as u64,
            value.segment_index as u64,
        ]
    }));
    matrix_values.extend(matrix.spans.iter().flat_map(|value| {
        [
            value.segment_index as u64,
            value.row_start as u64,
            value.row_stop as u64,
            value.column_start as u64,
            value.column_stop as u64,
        ]
    }));
    matrix_values.extend(
        matrix
            .horizontal_rule_rows
            .iter()
            .map(|value| *value as u64),
    );
    matrix_values.extend(
        matrix
            .vertical_rule_columns
            .iter()
            .map(|value| *value as u64),
    );
    let limit_count = analysis
        .partition_nodes
        .iter()
        .filter(|node| node.stop_reason == Some(geometry::PartitionStopReason::Limit))
        .count();
    let values = [
        analysis.foreground.width as u64,
        analysis.foreground.height as u64,
        analysis.rules.len() as u64,
        analysis.materialized_segments.segments.len() as u64,
        analysis.materialized_nodes.len() as u64,
        matrix.rows.len() as u64,
        matrix.columns.len() as u64,
        matrix.cells.len() as u64,
        matrix.spans.len() as u64,
        geometry_fnv_bytes(&analysis.foreground.pixels),
        geometry_fnv_bytes(&analysis.ownership_foreground),
        geometry_fnv_bytes(&analysis.rule_mask),
        geometry_fnv_values(
            analysis
                .materialized_segments
                .ownership
                .iter()
                .map(|owner| (*owner + 1) as u64),
        ),
        geometry_fnv_values(rule_values),
        geometry_fnv_values(segment_values),
        geometry_fnv_values(node_values),
        geometry_fnv_values(matrix_values),
        limit_count as u64,
        analysis.recovered_rule_count as u64,
        analysis
            .ownership_foreground
            .iter()
            .filter(|value| **value != 0)
            .count() as u64,
    ];
    let output = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    output.copy_from_slice(&values);
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_analyze_objects_fingerprint_copy(
    pixels: *const u8,
    pixel_length: u32,
    width: u32,
    height: u32,
    stride: u32,
    channels: u32,
    output: *mut u64,
    output_length: u32,
) -> i32 {
    if pixels.is_null() || output.is_null() || output_length != 8 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(pixels, pixel_length as usize) };
    let Some(analysis) = geometry::analyze_geometry(
        source,
        width as usize,
        height as usize,
        stride as usize,
        channels as usize,
    ) else {
        return -2;
    };
    let Some(reconstruction) = objects::reconstruct_objects(&analysis) else {
        return -3;
    };
    let mut object_values = Vec::<u64>::new();
    for item in &reconstruction.objects {
        object_values.push(match item.kind {
            objects::ObjectKind::Paragraph => 1,
            objects::ObjectKind::List => 2,
            objects::ObjectKind::Table => 3,
            // Legacy packed diagnostics retain their four-category encoding.
            objects::ObjectKind::Unknown | objects::ObjectKind::Flow => 4,
        });
        object_values.extend(item.bbox.map(|value| value as u64));
        object_values.extend([
            item.reading_index as u64,
            item.row_start as u64,
            item.row_stop as u64,
            item.column_start as u64,
            item.column_stop as u64,
            (item.confidence * 1_000_000.0).round_ties_even() as u64,
            item.segment_indexes.len() as u64,
        ]);
        object_values.extend(item.segment_indexes.iter().map(|value| *value as u64));
    }
    let values = [
        reconstruction.source_segment_indexes.len() as u64,
        reconstruction.objects.len() as u64,
        reconstruction
            .objects
            .iter()
            .filter(|item| item.kind == objects::ObjectKind::Paragraph)
            .count() as u64,
        reconstruction
            .objects
            .iter()
            .filter(|item| item.kind == objects::ObjectKind::List)
            .count() as u64,
        reconstruction
            .objects
            .iter()
            .filter(|item| item.kind == objects::ObjectKind::Table)
            .count() as u64,
        reconstruction
            .objects
            .iter()
            .filter(|item| matches!(item.kind, objects::ObjectKind::Unknown | objects::ObjectKind::Flow))
            .count() as u64,
        geometry_fnv_values(object_values),
        geometry_fnv_values(
            reconstruction
                .segment_ownership
                .iter()
                .map(|value| *value as u64),
        ),
    ];
    let output = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    output.copy_from_slice(&values);
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_analyze_blocks_fingerprint_copy(
    pixels: *const u8,
    pixel_length: u32,
    width: u32,
    height: u32,
    stride: u32,
    channels: u32,
    output: *mut u64,
    output_length: u32,
) -> i32 {
    if pixels.is_null() || output.is_null() || output_length != 10 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(pixels, pixel_length as usize) };
    let Some(analysis) = geometry::analyze_geometry(
        source,
        width as usize,
        height as usize,
        stride as usize,
        channels as usize,
    ) else {
        return -2;
    };
    let Some(reconstruction) = objects::reconstruct_objects(&analysis) else {
        return -3;
    };
    let Some(plan) = blocks::plan_blocks(&analysis, &reconstruction) else {
        return -4;
    };
    let mut block_values = Vec::<u64>::new();
    for block in &plan.blocks {
        block_values.extend(block.bbox.map(|value| value as u64));
        block_values.extend([
            block.scope_index as u64,
            u64::from(block.dyadic_mask),
            u64::from(block.matrix_window.is_some()),
        ]);
        block_values.extend(
            block
                .matrix_window
                .unwrap_or([0; 4])
                .map(|value| value as u64),
        );
        block_values.push(u64::from(block.matrix_segment_shape.is_some()));
        block_values.extend(
            block
                .matrix_segment_shape
                .unwrap_or([0; 2])
                .map(|value| value as u64),
        );
        for values in [
            &block.core_segment_indexes,
            &block.segment_indexes,
            &block.context_segment_indexes,
            &block.object_indexes,
        ] {
            block_values.push(values.len() as u64);
            block_values.extend(values.iter().map(|value| *value as u64));
        }
    }
    let mut algebra_values = Vec::<u64>::new();
    for item in &plan.algebra {
        algebra_values.extend([
            item.first_block_index as u64,
            item.second_block_index as u64,
        ]);
        for values in [
            &item.intersection_segment_indexes,
            &item.union_segment_indexes,
            &item.xor_segment_indexes,
            &item.first_only_segment_indexes,
            &item.second_only_segment_indexes,
        ] {
            algebra_values.push(values.len() as u64);
            algebra_values.extend(values.iter().map(|value| *value as u64));
        }
    }
    let memberships = plan
        .blocks
        .iter()
        .map(|block| block.segment_indexes.len())
        .sum::<usize>();
    let membership_counts = plan.source_segment_indexes.iter().map(|segment_index| {
        plan.blocks
            .iter()
            .filter(|block| block.segment_indexes.contains(segment_index))
            .count() as u64
    });
    let values = [
        plan.source_segment_indexes.len() as u64,
        reconstruction.objects.len() as u64,
        plan.blocks.len() as u64,
        plan.blocks.iter().filter(|block| block.dyadic_mask).count() as u64,
        plan.algebra.len() as u64,
        memberships as u64,
        plan.blocks
            .iter()
            .map(|block| block.segment_indexes.len())
            .max()
            .unwrap_or(0) as u64,
        geometry_fnv_values(block_values),
        geometry_fnv_values(algebra_values),
        geometry_fnv_values(membership_counts),
    ];
    let output = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    output.copy_from_slice(&values);
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_analyze_compact_fingerprint_copy(
    pixels: *const u8,
    pixel_length: u32,
    width: u32,
    height: u32,
    stride: u32,
    channels: u32,
    output: *mut u64,
    output_length: u32,
) -> i32 {
    if pixels.is_null() || output.is_null() || output_length != 12 {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(pixels, pixel_length as usize) };
    let Some(analysis) = geometry::analyze_geometry(
        source,
        width as usize,
        height as usize,
        stride as usize,
        channels as usize,
    ) else {
        return -2;
    };
    let Some(reconstruction) = objects::reconstruct_objects(&analysis) else {
        return -3;
    };
    let Some(plan) = blocks::plan_blocks(&analysis, &reconstruction) else {
        return -4;
    };
    let Some(compacted) = compact::compact_blocks(&analysis, &plan, &reconstruction.objects) else {
        return -5;
    };
    let mut placement_values = Vec::<u64>::new();
    let mut metadata_values = Vec::<u64>::new();
    let mut pixel_values = Vec::<u64>::new();
    for block in &compacted {
        metadata_values.extend([
            block.width as u64,
            block.height as u64,
            block.placements.len() as u64,
            block.omitted_unit_indexes.len() as u64,
            block.occupied_pixels_before as u64,
            block.occupied_pixels_after as u64,
            block.packed_canvas_pixels as u64,
        ]);
        metadata_values.extend(block.omitted_unit_indexes.iter().map(|value| *value as u64));
        pixel_values.extend([
            block.width as u64,
            block.height as u64,
            geometry_fnv_bytes(&block.pixels),
        ]);
        for placement in &block.placements {
            placement_values.push(placement.unit_index as u64);
            placement_values.push(placement.segment_indexes.len() as u64);
            placement_values.extend(placement.segment_indexes.iter().map(|value| *value as u64));
            placement_values.extend(placement.source_bbox.map(|value| value as u64));
            placement_values.extend(placement.crop_bbox.map(|value| value as u64));
        }
    }
    let values = [
        plan.source_segment_indexes.len() as u64,
        plan.blocks.len() as u64,
        compacted.len() as u64,
        compacted
            .iter()
            .map(|item| item.placements.len())
            .sum::<usize>() as u64,
        compacted
            .iter()
            .map(|item| item.omitted_unit_indexes.len())
            .sum::<usize>() as u64,
        compacted
            .iter()
            .map(|item| item.occupied_pixels_before)
            .sum::<usize>() as u64,
        compacted
            .iter()
            .map(|item| item.packed_canvas_pixels)
            .sum::<usize>() as u64,
        compacted.iter().map(|item| item.width).max().unwrap_or(0) as u64,
        compacted.iter().map(|item| item.height).max().unwrap_or(0) as u64,
        geometry_fnv_values(metadata_values),
        geometry_fnv_values(placement_values),
        geometry_fnv_values(pixel_values),
    ];
    let output = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    output.copy_from_slice(&values);
    0
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn ittm_analyze_compact_block_hashes_copy(
    pixels: *const u8,
    pixel_length: u32,
    width: u32,
    height: u32,
    stride: u32,
    channels: u32,
    output: *mut u64,
    output_length: u32,
) -> i32 {
    if pixels.is_null() || output.is_null() {
        return -1;
    }
    let source = unsafe { std::slice::from_raw_parts(pixels, pixel_length as usize) };
    let Some(analysis) = geometry::analyze_geometry(
        source,
        width as usize,
        height as usize,
        stride as usize,
        channels as usize,
    ) else {
        return -2;
    };
    let Some(reconstruction) = objects::reconstruct_objects(&analysis) else {
        return -3;
    };
    let Some(plan) = blocks::plan_blocks(&analysis, &reconstruction) else {
        return -4;
    };
    let Some(compacted) = compact::compact_blocks(&analysis, &plan, &reconstruction.objects) else {
        return -5;
    };
    if output_length as usize != compacted.len() * 3 {
        return compacted.len() as i32;
    }
    let output = unsafe { std::slice::from_raw_parts_mut(output, output_length as usize) };
    for (target, block) in output.chunks_exact_mut(3).zip(&compacted) {
        target.copy_from_slice(&[
            block.width as u64,
            block.height as u64,
            geometry_fnv_bytes(&block.pixels),
        ]);
    }
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sparse_codes_match_the_pipeline_contract() {
        assert_eq!(add_sparse_signal(0, MERGE_UP_CODE), Ok(3));
        assert_eq!(add_sparse_signal(3, MERGE_LEFT_CODE), Ok(8));
        assert_eq!(add_sparse_signal(3, EMPTY_SLOT_CODE), Ok(14));
        assert_eq!(add_sparse_signal(5, EMPTY_SLOT_CODE), Ok(16));
        assert_eq!(add_sparse_signal(8, EMPTY_SLOT_CODE), Ok(19));
        assert_eq!(add_sparse_signal(14, MERGE_UP_CODE), Ok(14));
    }

    #[test]
    fn trusted_markdown_api_runs_only_recognition() {
        let capabilities = PipelineCapabilities {
            trusted_text: true,
            provides_layout: true,
            provides_markdown: true,
            needs_language_retry: false,
        };
        assert_eq!(recipe_mask(capabilities), Stage::RecognizeSegments.bit());
    }

    #[test]
    fn local_ocr_runs_the_complete_recipe() {
        assert_eq!(recipe_mask(PipelineCapabilities::default()), u8::MAX.into());
    }

    #[test]
    fn sparse_errors_match_python_validation_order() {
        assert_eq!(
            add_sparse_signal(1, 1),
            Err(SparseCodeError::UnknownSignal(1))
        );
        assert_eq!(
            add_sparse_signal(1, MERGE_UP_CODE),
            Err(SparseCodeError::UnknownCode(1))
        );
    }

    #[test]
    fn image_plane_validates_stride_and_payload() {
        let image = ImagePlane {
            width: 2,
            height: 2,
            stride: 6,
            format: PixelFormat::Rgb8,
            pixels: vec![255; 12],
        };
        assert!(image.is_valid());

        let truncated = ImagePlane {
            pixels: vec![255; 11],
            ..image
        };
        assert!(!truncated.is_valid());
    }

    #[test]
    fn isolated_heading_requires_short_content_and_both_boundaries() {
        assert!(is_isolated_heading(2, 18, 12, true, true));
        assert!(!is_isolated_heading(1, 2, 2, true, true));
        assert!(!is_isolated_heading(3, 18, 8, true, true));
        assert!(!is_isolated_heading(1, 18, 8, true, false));
    }

    #[test]
    fn primary_replacement_allows_only_substantial_fallback_for_tiny_input() {
        assert!(!should_replace_primary(0, 79, 0, 0));
        assert!(should_replace_primary(0, 80, 0, 0));
        assert!(should_replace_primary(9, 80, 0, u32::MAX));
        assert!(should_replace_primary(1, 6, 1, 0));
        assert!(!should_replace_primary(1, 3, 1, 0));
        assert!(!should_replace_primary(3, 9, 1, 1));
    }

    #[test]
    fn primary_replacement_requires_growth_and_token_retention() {
        assert!(!should_replace_primary(100, 179, 5, 5));
        assert!(should_replace_primary(100, 180, 5, 4));
        assert!(!should_replace_primary(100, 180, 5, 3));
        assert!(!should_replace_primary(200, 279, 5, 5));
        assert!(should_replace_primary(200, 280, 5, 5));
        assert!(!should_replace_primary(129, 180, 5, 5));
        assert!(should_replace_primary(129, 181, 5, 5));
    }

    #[test]
    fn primary_replacement_rejects_missing_tokens_and_handles_extreme_counts() {
        assert!(!should_replace_primary(10, 180, 0, 0));
        assert!(should_replace_primary(10, 180, 5, u32::MAX));
        assert!(!should_replace_primary(
            u32::MAX,
            u32::MAX,
            u32::MAX,
            u32::MAX,
        ));
    }

    #[test]
    fn duplicate_drop_requires_two_substantial_blocks() {
        assert!(!should_drop_text_block(31, 100, 10, 10, 10, 1_000));
        assert!(!should_drop_text_block(100, 31, 10, 10, 10, 1_000));
        assert!(should_drop_text_block(32, 32, 0, 0, 0, 880));
    }

    #[test]
    fn duplicate_drop_accepts_similarity_or_incoming_token_coverage() {
        assert!(!should_drop_text_block(100, 100, 0, 0, 0, 879));
        assert!(should_drop_text_block(100, 100, 0, 0, 0, 880));
        assert!(should_drop_text_block(100, 100, 17, 20, 20, 0));
        assert!(!should_drop_text_block(100, 100, 16, 20, 20, 0));
        assert!(should_drop_text_block(100, 100, 17, 20, 21, 0));
        assert!(!should_drop_text_block(100, 100, 17, 21, 20, 0));
    }

    #[test]
    fn duplicate_drop_clamps_untrusted_ffi_metrics() {
        assert!(should_drop_text_block(100, 100, 0, 0, 0, u32::MAX));
        assert!(should_drop_text_block(100, 100, u32::MAX, 10, 12, 0));
        assert!(should_drop_text_block(100, 100, u32::MAX, 10, 10, 0));
    }

    #[test]
    fn abi_exposes_the_new_deterministic_decisions() {
        assert_eq!(ittm_pipeline_abi_version(), 6);
        assert_eq!(ittm_should_replace_primary(100, 180, 5, 4), 1);
        assert_eq!(ittm_should_replace_primary(100, 180, 5, 3), 0);
        assert_eq!(ittm_should_drop_text_block(32, 32, 17, 20, 20, 0), 1);
        assert_eq!(ittm_should_drop_text_block(32, 32, 16, 20, 20, 0), 0);
    }
}
