use std::cmp::Ordering;

pub const LOCK_GRAMMAR_PERCENT: u8 = 97;
pub const GAMMA_DARK: f32 = 1.2;
const DARK_BACKGROUND_MAX: f64 = 96.0;
const DARK_TEXT_CONTRAST_MIN: f64 = 96.0;
const DARK_TEXT_FOREGROUND_MIN: f64 = 0.003;
const DARK_TEXT_FOREGROUND_MAX: f64 = 0.05;
const DARK_TEXT_COMPONENTS_MIN: usize = 8;
const DARK_TEXT_X_HEIGHT_MIN: f64 = 5.0;
const DARK_TEXT_X_HEIGHT_MAX: f64 = 14.0;
const DARK_TEXT_MAX_PIXELS: usize = 16_000_000;

pub fn gamma_dark_rgb(source: &[u8], channels: usize) -> Option<Vec<u8>> {
    if !matches!(channels, 3 | 4) || source.len() % channels != 0 {
        return None;
    }
    let mut output = Vec::with_capacity(source.len() / channels);
    for pixel in source.chunks_exact(channels) {
        let red = f32::from(pixel[0]) / 255.0_f32;
        let green = f32::from(pixel[1]) / 255.0_f32;
        let blue = f32::from(pixel[2]) / 255.0_f32;
        let mut grayscale = red * 0.299_f32;
        grayscale += green * 0.587_f32;
        grayscale += blue * 0.114_f32;
        let value =
            (grayscale.powf(GAMMA_DARK).clamp(0.0, 1.0) * 255.0_f32).round_ties_even() as u8;
        output.push(value);
    }
    Some(output)
}

fn percentile(values: &[u8], quantile: f64) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    let mut ordered = values.to_vec();
    ordered.sort_unstable();
    let rank = (ordered.len().saturating_sub(1) as f64) * quantile;
    let lower = rank.floor() as usize;
    let upper = rank.ceil() as usize;
    let fraction = rank - lower as f64;
    Some(f64::from(ordered[lower]) * (1.0 - fraction) + f64::from(ordered[upper]) * fraction)
}

fn otsu_foreground(grayscale: &[u8]) -> Vec<bool> {
    let mut histogram = [0_u64; 256];
    for value in grayscale {
        histogram[usize::from(*value)] += 1;
    }
    let total = grayscale.len() as f64;
    let weighted_total = histogram
        .iter()
        .enumerate()
        .map(|(value, count)| value as f64 * *count as f64)
        .sum::<f64>();
    let mut background_weight = 0.0_f64;
    let mut background_sum = 0.0_f64;
    let mut best_variance = -1.0_f64;
    let mut best_threshold = 0_u8;
    for (threshold, count) in histogram.into_iter().enumerate() {
        background_weight += count as f64;
        if background_weight <= 0.0 {
            continue;
        }
        let foreground_weight = total - background_weight;
        if foreground_weight <= 0.0 {
            break;
        }
        background_sum += threshold as f64 * count as f64;
        let background_mean = background_sum / background_weight;
        let foreground_mean = (weighted_total - background_sum) / foreground_weight;
        let variance =
            background_weight * foreground_weight * (background_mean - foreground_mean).powi(2);
        if variance > best_variance {
            best_variance = variance;
            best_threshold = threshold as u8;
        }
    }
    grayscale
        .iter()
        .map(|value| *value > best_threshold)
        .collect()
}

fn foreground_component_stats(
    foreground: &[bool],
    width: usize,
    height: usize,
) -> Vec<(usize, usize, usize)> {
    #[derive(Clone, Copy)]
    struct Run {
        left: usize,
        right: usize,
        top: usize,
        area: usize,
    }

    fn find(parent: &mut [usize], mut value: usize) -> usize {
        while parent[value] != value {
            parent[value] = parent[parent[value]];
            value = parent[value];
        }
        value
    }

    fn union(parent: &mut [usize], first: usize, second: usize) {
        let first_root = find(parent, first);
        let second_root = find(parent, second);
        if first_root != second_root {
            parent[second_root] = first_root;
        }
    }

    let mut parent = Vec::<usize>::new();
    let mut runs = Vec::<Run>::new();
    let mut previous = Vec::<(usize, usize, usize)>::new();
    for top in 0..height {
        let row = &foreground[top * width..(top + 1) * width];
        let mut current = Vec::<(usize, usize, usize)>::new();
        let mut left = 0_usize;
        let mut previous_index = 0_usize;
        while left < width {
            while left < width && !row[left] {
                left += 1;
            }
            if left == width {
                break;
            }
            let mut right = left + 1;
            while right < width && row[right] {
                right += 1;
            }
            let run_id = runs.len();
            parent.push(run_id);
            runs.push(Run {
                left,
                right,
                top,
                area: right - left,
            });
            while previous_index < previous.len()
                && previous[previous_index].1.saturating_add(1) < left
            {
                previous_index += 1;
            }
            let mut overlap_index = previous_index;
            while overlap_index < previous.len() && previous[overlap_index].0 <= right {
                union(&mut parent, run_id, previous[overlap_index].2);
                overlap_index += 1;
            }
            current.push((left, right, run_id));
            left = right;
        }
        previous = current;
    }

    let mut aggregates = std::collections::BTreeMap::<usize, [usize; 5]>::new();
    for (run_id, run) in runs.into_iter().enumerate() {
        let root = find(&mut parent, run_id);
        let value =
            aggregates
                .entry(root)
                .or_insert([run.left, run.right, run.top, run.top + 1, 0]);
        value[0] = value[0].min(run.left);
        value[1] = value[1].max(run.right);
        value[2] = value[2].min(run.top);
        value[3] = value[3].max(run.top + 1);
        value[4] += run.area;
    }
    aggregates
        .into_values()
        .map(|value| (value[1] - value[0], value[3] - value[2], value[4]))
        .collect()
}

pub fn normalize_dark_small_text_rgb(
    source: &[u8],
    width: usize,
    height: usize,
) -> Option<Vec<u8>> {
    let pixels = width.checked_mul(height)?;
    if pixels == 0 || pixels > DARK_TEXT_MAX_PIXELS || source.len() != pixels.checked_mul(3)? {
        return None;
    }
    let grayscale = source
        .chunks_exact(3)
        .map(|pixel| {
            (f64::from(pixel[0]) * 0.299
                + f64::from(pixel[1]) * 0.587
                + f64::from(pixel[2]) * 0.114)
                .round_ties_even() as u8
        })
        .collect::<Vec<_>>();
    let background = percentile(&grayscale, 0.5)?;
    if background > DARK_BACKGROUND_MAX {
        return None;
    }
    let foreground = otsu_foreground(&grayscale);
    let contrast = percentile(&grayscale, 0.99)? - background;
    let foreground_ratio = foreground.iter().filter(|value| **value).count() as f64 / pixels as f64;
    if contrast < DARK_TEXT_CONTRAST_MIN
        || !(DARK_TEXT_FOREGROUND_MIN..=DARK_TEXT_FOREGROUND_MAX).contains(&foreground_ratio)
    {
        return None;
    }
    let maximum_component_width = 80_usize.min(2_usize.max(width / 3));
    let maximum_component_height = 40_usize.min(height);
    let component_heights = foreground_component_stats(&foreground, width, height)
        .into_iter()
        .filter_map(|(component_width, component_height, component_area)| {
            (2 <= component_width
                && component_width <= maximum_component_width
                && 3 <= component_height
                && component_height <= maximum_component_height
                && 3 <= component_area
                && component_area <= 1_000
                && component_width * component_height <= 7 * component_area)
                .then_some(component_height as u8)
        })
        .collect::<Vec<_>>();
    if component_heights.len() < DARK_TEXT_COMPONENTS_MIN {
        return None;
    }
    let x_height = percentile(&component_heights, 0.75)?;
    if !(DARK_TEXT_X_HEIGHT_MIN..=DARK_TEXT_X_HEIGHT_MAX).contains(&x_height) {
        return None;
    }
    gamma_dark_rgb(source, 3)
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
#[repr(u8)]
pub enum LanguageProfileId {
    RusEng = 0,
    Rus = 1,
    Eng = 2,
    ChiSim = 3,
    Ell = 4,
    Equ = 5,
}

impl LanguageProfileId {
    pub const ALL: [Self; 6] = [
        Self::RusEng,
        Self::Rus,
        Self::Eng,
        Self::ChiSim,
        Self::Ell,
        Self::Equ,
    ];

    pub const fn code(self) -> &'static str {
        match self {
            Self::RusEng => "rus+eng",
            Self::Rus => "rus",
            Self::Eng => "eng",
            Self::ChiSim => "chi_sim",
            Self::Ell => "ell",
            Self::Equ => "equ",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub enum OcrTransform {
    Raw = 0,
    GammaDark = 1,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LanguageRequest {
    pub profile: LanguageProfileId,
    pub transform: OcrTransform,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LanguageObservation {
    pub grammar_percent: u8,
    pub mean_confidence_milli: u16,
}

impl LanguageObservation {
    pub fn bounded(grammar_percent: u32, mean_confidence_milli: u32) -> Self {
        Self {
            grammar_percent: grammar_percent.min(100) as u8,
            mean_confidence_milli: mean_confidence_milli.min(1_000) as u16,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SplayLanguageNode {
    pub profile: LanguageProfileId,
    pub initial_rank: u8,
    pub quality_sum_percent: u64,
    pub attempts: u32,
    pub exact_grammar: u32,
    pub garbage: u32,
    pub last_grammar_percent: u8,
}

impl SplayLanguageNode {
    fn weight_numerator(&self) -> u128 {
        u128::from(100 + self.quality_sum_percent)
    }

    fn weight_denominator(&self) -> u128 {
        u128::from(2 + self.attempts)
    }
}

fn descending_ratio(
    left_numerator: u128,
    left_denominator: u128,
    right_numerator: u128,
    right_denominator: u128,
) -> Ordering {
    (right_numerator * left_denominator).cmp(&(left_numerator * right_denominator))
}

fn compare_nodes(left: &SplayLanguageNode, right: &SplayLanguageNode) -> Ordering {
    right
        .exact_grammar
        .cmp(&left.exact_grammar)
        .then_with(|| {
            descending_ratio(
                u128::from(left.exact_grammar),
                u128::from(left.attempts.max(1)),
                u128::from(right.exact_grammar),
                u128::from(right.attempts.max(1)),
            )
        })
        .then_with(|| left.garbage.cmp(&right.garbage))
        .then_with(|| {
            descending_ratio(
                left.weight_numerator(),
                left.weight_denominator(),
                right.weight_numerator(),
                right.weight_denominator(),
            )
        })
        .then_with(|| left.initial_rank.cmp(&right.initial_rank))
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct LanguageSplayState {
    nodes: Vec<SplayLanguageNode>,
    pub locked_profile: Option<LanguageProfileId>,
    pub lock_is_provisional: bool,
}

impl Default for LanguageSplayState {
    fn default() -> Self {
        Self {
            nodes: LanguageProfileId::ALL
                .into_iter()
                .enumerate()
                .map(|(rank, profile)| SplayLanguageNode {
                    profile,
                    initial_rank: rank as u8,
                    quality_sum_percent: 0,
                    attempts: 0,
                    exact_grammar: 0,
                    garbage: 0,
                    last_grammar_percent: 0,
                })
                .collect(),
            locked_profile: None,
            lock_is_provisional: false,
        }
    }
}

impl LanguageSplayState {
    pub fn nodes(&self) -> &[SplayLanguageNode] {
        &self.nodes
    }

    pub fn ordered_profiles(&self) -> Vec<LanguageProfileId> {
        self.nodes.iter().map(|node| node.profile).collect()
    }

    pub fn observe(&mut self, profile: LanguageProfileId, grammar_percent: u8) {
        let grammar_percent = grammar_percent.min(100);
        let node = self
            .nodes
            .iter_mut()
            .find(|node| node.profile == profile)
            .expect("all language profiles are present in the splay tree");
        node.attempts += 1;
        node.quality_sum_percent += u64::from(grammar_percent);
        node.last_grammar_percent = grammar_percent;
        if grammar_percent == 100 {
            node.exact_grammar += 1;
        } else if grammar_percent < 25 {
            node.garbage += 1;
        }
        self.nodes.sort_by(compare_nodes);
    }

    pub fn initialize_document_lock(
        &mut self,
        winner: LanguageProfileId,
        primary_grammar_percent: u8,
    ) {
        if self.locked_profile.is_none() {
            self.locked_profile = Some(winner);
            self.lock_is_provisional = primary_grammar_percent < LOCK_GRAMMAR_PERCENT;
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LanguageAgendaResult {
    Request(LanguageRequest),
    Complete {
        winner_request: LanguageRequest,
        winner_profile: LanguageProfileId,
        winner_observation: LanguageObservation,
    },
    Exhausted {
        winner_request: LanguageRequest,
        winner_profile: LanguageProfileId,
        winner_observation: LanguageObservation,
    },
}

#[derive(Clone, Debug)]
pub struct LanguageAgenda {
    bootstrap: bool,
    specialized_transform_retry: bool,
    profiles: Vec<LanguageProfileId>,
    profile_index: usize,
    pending: LanguageRequest,
    primary_grammar_percent: u8,
    profile_best: Option<LanguageObservation>,
    winner: Option<(LanguageRequest, LanguageObservation)>,
}

const EQUIVALENT_GRAMMAR_MARGIN_PERCENT: u8 = 2;

fn observation_score(observation: LanguageObservation) -> (u8, u16) {
    (
        observation.grammar_percent,
        observation.mean_confidence_milli,
    )
}

fn candidate_is_better(
    candidate: LanguageObservation,
    candidate_rank: u8,
    current: LanguageObservation,
    current_rank: u8,
) -> bool {
    if candidate.grammar_percent
        > current
            .grammar_percent
            .saturating_add(EQUIVALENT_GRAMMAR_MARGIN_PERCENT)
    {
        return true;
    }
    if current.grammar_percent
        > candidate
            .grammar_percent
            .saturating_add(EQUIVALENT_GRAMMAR_MARGIN_PERCENT)
    {
        return false;
    }
    if candidate_rank != current_rank {
        return candidate_rank < current_rank;
    }
    observation_score(candidate) > observation_score(current)
}

impl LanguageAgenda {
    #[cfg(test)]
    pub fn begin(state: &LanguageSplayState) -> Self {
        Self::begin_for_context(state, true)
    }

    pub fn begin_for_context(state: &LanguageSplayState, local_membership: bool) -> Self {
        let bootstrap = state.locked_profile.is_none();
        let primary = state.locked_profile.unwrap_or(LanguageProfileId::RusEng);
        let mut profiles = vec![primary];
        if bootstrap || local_membership {
            profiles.extend(
                state
                    .ordered_profiles()
                    .into_iter()
                    .filter(|profile| *profile != primary),
            );
        } else if primary != LanguageProfileId::RusEng {
            profiles.push(LanguageProfileId::RusEng);
        }
        Self {
            bootstrap,
            specialized_transform_retry: !local_membership,
            profiles,
            profile_index: 0,
            pending: LanguageRequest {
                profile: primary,
                transform: OcrTransform::Raw,
            },
            primary_grammar_percent: 0,
            profile_best: None,
            winner: None,
        }
    }

    pub fn begin_for_context_with_fallbacks(
        state: &LanguageSplayState,
        local_membership: bool,
        evidenced_profiles: &[LanguageProfileId],
    ) -> Self {
        let bootstrap = state.locked_profile.is_none();
        if bootstrap {
            return Self::begin_for_context(state, local_membership);
        }
        let primary = state.locked_profile.unwrap_or(LanguageProfileId::RusEng);
        let mut profiles = vec![primary];
        if primary != LanguageProfileId::RusEng {
            profiles.push(LanguageProfileId::RusEng);
        }
        let ordered = state.ordered_profiles();
        for profile in &ordered {
            if evidenced_profiles.contains(profile) && !profiles.contains(profile) {
                profiles.push(*profile);
            }
        }
        for profile in ordered {
            if !profiles.contains(&profile) {
                profiles.push(profile);
            }
        }
        Self {
            bootstrap,
            specialized_transform_retry: !local_membership,
            profiles,
            profile_index: 0,
            pending: LanguageRequest {
                profile: primary,
                transform: OcrTransform::Raw,
            },
            primary_grammar_percent: 0,
            profile_best: None,
            winner: None,
        }
    }

    pub const fn request(&self) -> LanguageRequest {
        self.pending
    }

    fn retain_candidate(&mut self, request: LanguageRequest, observation: LanguageObservation) {
        let rank = self
            .profiles
            .iter()
            .position(|profile| *profile == request.profile)
            .unwrap_or(usize::MAX) as u8;
        if self
            .profile_best
            .is_none_or(|current| observation_score(observation) > observation_score(current))
        {
            self.profile_best = Some(observation);
        }
        let replace_winner = self.winner.is_none_or(|(winner_request, current)| {
            let winner_rank = self
                .profiles
                .iter()
                .position(|profile| *profile == winner_request.profile)
                .unwrap_or(usize::MAX) as u8;
            candidate_is_better(observation, rank, current, winner_rank)
        });
        if replace_winner {
            self.winner = Some((request, observation));
        }
    }

    fn finish_profile(&mut self, state: &mut LanguageSplayState) {
        let profile = self.profiles[self.profile_index];
        let grammar = self
            .profile_best
            .map_or(0, |observation| observation.grammar_percent);
        if self.profile_index == 0 {
            self.primary_grammar_percent = grammar;
        }
        state.observe(profile, grammar);
    }

    pub fn observe(
        &mut self,
        state: &mut LanguageSplayState,
        observation: LanguageObservation,
    ) -> LanguageAgendaResult {
        let request = self.pending;
        self.retain_candidate(request, observation);

        let specialized_retry = matches!(
            request.profile,
            LanguageProfileId::ChiSim | LanguageProfileId::Ell | LanguageProfileId::Equ
        ) && self.specialized_transform_retry
            && observation.grammar_percent > 0;
        if request.transform == OcrTransform::Raw
            && observation.grammar_percent < LOCK_GRAMMAR_PERCENT
            && (self.profile_index == 0 || specialized_retry)
        {
            self.pending.transform = OcrTransform::GammaDark;
            return LanguageAgendaResult::Request(self.pending);
        }

        self.finish_profile(state);
        let profile_best = self
            .profile_best
            .expect("the current profile has an observation");
        let profile_reached_lock = profile_best.grammar_percent >= LOCK_GRAMMAR_PERCENT;
        let primary_reached_lock = self.profile_index == 0 && profile_reached_lock;
        if primary_reached_lock {
            let (winner_request, winner_observation) = self.winner.expect("a winner exists");
            let winner_profile = winner_request.profile;
            if self.bootstrap {
                state.initialize_document_lock(winner_profile, self.primary_grammar_percent);
            }
            return LanguageAgendaResult::Complete {
                winner_request,
                winner_profile,
                winner_observation,
            };
        }

        let should_stop_on_fallback =
            !self.bootstrap && self.profile_index > 0 && profile_reached_lock;
        if should_stop_on_fallback || self.profile_index + 1 == self.profiles.len() {
            let (winner_request, winner_observation) = self.winner.expect("a winner exists");
            let winner_profile = winner_request.profile;
            if self.bootstrap {
                state.initialize_document_lock(winner_profile, self.primary_grammar_percent);
            }
            return if winner_observation.grammar_percent >= LOCK_GRAMMAR_PERCENT {
                LanguageAgendaResult::Complete {
                    winner_request,
                    winner_profile,
                    winner_observation,
                }
            } else {
                LanguageAgendaResult::Exhausted {
                    winner_request,
                    winner_profile,
                    winner_observation,
                }
            };
        }

        self.profile_index += 1;
        self.profile_best = None;
        self.pending = LanguageRequest {
            profile: self.profiles[self.profile_index],
            transform: OcrTransform::Raw,
        };
        LanguageAgendaResult::Request(self.pending)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dark_sparse_xheight_gate_normalizes_without_resizing() {
        let width = 200_usize;
        let height = 50_usize;
        let mut source = vec![20_u8; width * height * 3];
        for component in 0..10 {
            let left = 8 + component * 18;
            for top in 20..28 {
                for x in left..left + 3 {
                    let offset = (top * width + x) * 3;
                    source[offset..offset + 3].fill(220);
                }
            }
        }
        let normalized = normalize_dark_small_text_rgb(&source, width, height)
            .expect("dark sparse text must activate the frozen gate");
        assert_eq!(normalized.len(), width * height);
        assert_eq!(normalized, gamma_dark_rgb(&source, 3).unwrap());

        let light = vec![240_u8; width * height * 3];
        assert!(normalize_dark_small_text_rgb(&light, width, height).is_none());
    }

    fn score(grammar: u32) -> LanguageObservation {
        LanguageObservation::bounded(grammar, 920)
    }

    #[test]
    fn gamma_dark_matches_the_frozen_python_float32_recipe() {
        let source = [
            0, 0, 0, 255, 255, 255, 64, 64, 64, 255, 0, 0, 0, 255, 0, 0, 0, 255, 12, 34, 56, 240,
            220, 200,
        ];
        assert_eq!(
            gamma_dark_rgb(&source, 3),
            Some(vec![0, 255, 49, 60, 135, 19, 19, 218])
        );
    }

    #[test]
    fn gamma_dark_preserves_binary_pixels() {
        assert_eq!(
            gamma_dark_rgb(&[0, 0, 0, 255, 255, 255], 3),
            Some(vec![0, 255])
        );
    }

    #[test]
    fn exact_python_splay_order_is_preserved() {
        let mut state = LanguageSplayState::default();
        state.observe(LanguageProfileId::RusEng, 50);
        state.observe(LanguageProfileId::Rus, 80);
        state.observe(LanguageProfileId::Eng, 70);
        state.observe(LanguageProfileId::ChiSim, 95);
        state.observe(LanguageProfileId::Ell, 60);
        state.observe(LanguageProfileId::Equ, 55);
        assert_eq!(
            state.ordered_profiles(),
            vec![
                LanguageProfileId::ChiSim,
                LanguageProfileId::Rus,
                LanguageProfileId::Eng,
                LanguageProfileId::Ell,
                LanguageProfileId::Equ,
                LanguageProfileId::RusEng,
            ]
        );
    }

    #[test]
    fn target_raw_skips_gamma_and_confirms_default_lock() {
        let mut state = LanguageSplayState::default();
        let mut agenda = LanguageAgenda::begin(&state);
        assert_eq!(agenda.request().transform, OcrTransform::Raw);
        assert!(matches!(
            agenda.observe(&mut state, score(97)),
            LanguageAgendaResult::Complete {
                winner_profile: LanguageProfileId::RusEng,
                ..
            }
        ));
        assert_eq!(state.locked_profile, Some(LanguageProfileId::RusEng));
        assert!(!state.lock_is_provisional);
    }

    #[test]
    fn low_first_block_runs_gamma_then_sweeps_every_profile() {
        let mut state = LanguageSplayState::default();
        let mut agenda = LanguageAgenda::begin(&state);
        let expected = [
            (LanguageProfileId::RusEng, OcrTransform::GammaDark, 50),
            (LanguageProfileId::Rus, OcrTransform::Raw, 80),
            (LanguageProfileId::Eng, OcrTransform::Raw, 70),
            (LanguageProfileId::ChiSim, OcrTransform::Raw, 95),
            (LanguageProfileId::Ell, OcrTransform::Raw, 60),
            (LanguageProfileId::Equ, OcrTransform::Raw, 55),
        ];
        let mut result = agenda.observe(&mut state, score(50));
        for (profile, transform, grammar) in expected {
            assert_eq!(
                result,
                LanguageAgendaResult::Request(LanguageRequest { profile, transform })
            );
            result = agenda.observe(&mut state, score(grammar));
        }
        assert!(matches!(
            result,
            LanguageAgendaResult::Exhausted {
                winner_profile: LanguageProfileId::ChiSim,
                ..
            }
        ));
        assert_eq!(state.locked_profile, Some(LanguageProfileId::ChiSim));
        assert!(state.lock_is_provisional);
    }

    #[test]
    fn mixed_text_retries_a_specialized_profile_with_gamma() {
        let mut state = LanguageSplayState::default();
        let mut agenda = LanguageAgenda::begin_for_context(&state, false);
        let expected = [
            LanguageRequest {
                profile: LanguageProfileId::RusEng,
                transform: OcrTransform::GammaDark,
            },
            LanguageRequest {
                profile: LanguageProfileId::Rus,
                transform: OcrTransform::Raw,
            },
            LanguageRequest {
                profile: LanguageProfileId::Eng,
                transform: OcrTransform::Raw,
            },
            LanguageRequest {
                profile: LanguageProfileId::ChiSim,
                transform: OcrTransform::Raw,
            },
            LanguageRequest {
                profile: LanguageProfileId::ChiSim,
                transform: OcrTransform::GammaDark,
            },
        ];
        let mut result = agenda.observe(&mut state, score(50));
        for request in expected {
            assert_eq!(result, LanguageAgendaResult::Request(request));
            result = agenda.observe(&mut state, score(50));
        }
    }

    #[test]
    fn nearby_grammar_score_keeps_the_current_splay_priority() {
        let mut state = LanguageSplayState::default();
        let mut agenda = LanguageAgenda::begin(&state);
        let observations = [
            (77, 530),
            (75, 480),
            (52, 0),
            (77, 530),
            (40, 330),
            (78, 550),
            (62, 710),
        ];
        let mut result = None;
        for (grammar, confidence) in observations {
            result = Some(agenda.observe(
                &mut state,
                LanguageObservation::bounded(grammar, confidence),
            ));
        }
        assert!(matches!(
            result,
            Some(LanguageAgendaResult::Exhausted {
                winner_request: LanguageRequest {
                    profile: LanguageProfileId::RusEng,
                    transform: OcrTransform::Raw,
                },
                ..
            })
        ));
    }

    #[test]
    fn locked_block_stops_at_first_fallback_reaching_target() {
        let mut state = LanguageSplayState::default();
        state.locked_profile = Some(LanguageProfileId::RusEng);
        let mut agenda = LanguageAgenda::begin(&state);
        assert!(matches!(
            agenda.observe(&mut state, score(40)),
            LanguageAgendaResult::Request(LanguageRequest {
                profile: LanguageProfileId::RusEng,
                transform: OcrTransform::GammaDark,
            })
        ));
        let mut result = agenda.observe(&mut state, score(45));
        for (profile, grammar) in [
            (LanguageProfileId::Rus, 30),
            (LanguageProfileId::Eng, 50),
            (LanguageProfileId::ChiSim, 100),
        ] {
            assert_eq!(
                result,
                LanguageAgendaResult::Request(LanguageRequest {
                    profile,
                    transform: OcrTransform::Raw,
                })
            );
            result = agenda.observe(&mut state, score(grammar));
        }
        assert!(matches!(
            result,
            LanguageAgendaResult::Complete {
                winner_profile: LanguageProfileId::ChiSim,
                ..
            }
        ));
    }

    #[test]
    fn locked_whole_context_splays_after_primary_and_mixed_fallback() {
        let mut state = LanguageSplayState::default();
        state.locked_profile = Some(LanguageProfileId::Eng);
        let mut agenda =
            LanguageAgenda::begin_for_context_with_fallbacks(&state, false, &[]);
        assert_eq!(agenda.request().profile, LanguageProfileId::Eng);
        assert_eq!(agenda.request().transform, OcrTransform::Raw);
        assert!(matches!(
            agenda.observe(&mut state, score(80)),
            LanguageAgendaResult::Request(LanguageRequest {
                profile: LanguageProfileId::Eng,
                transform: OcrTransform::GammaDark,
            })
        ));
        assert!(matches!(
            agenda.observe(&mut state, score(82)),
            LanguageAgendaResult::Request(LanguageRequest {
                profile: LanguageProfileId::RusEng,
                transform: OcrTransform::Raw,
            })
        ));
        assert!(matches!(
            agenda.observe(&mut state, score(79)),
            LanguageAgendaResult::Request(LanguageRequest {
                profile: LanguageProfileId::Rus,
                transform: OcrTransform::Raw,
            })
        ));
        assert!(matches!(
            agenda.observe(&mut state, score(70)),
            LanguageAgendaResult::Request(LanguageRequest {
                profile: LanguageProfileId::ChiSim,
                transform: OcrTransform::Raw,
            })
        ));
        assert!(matches!(
            agenda.observe(&mut state, score(100)),
            LanguageAgendaResult::Complete {
                winner_profile: LanguageProfileId::ChiSim,
                ..
            }
        ));
    }
}
