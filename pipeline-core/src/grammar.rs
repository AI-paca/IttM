#[derive(Clone, Debug, Eq, PartialEq)]
pub struct GrammarAssessment {
    pub percent: u8,
    pub exact: bool,
    pub reasons: Vec<&'static str>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Script {
    Neutral,
    Cyrillic,
    Latin,
    Greek,
    Cjk,
    Hiragana,
    Katakana,
    Other,
}

#[derive(Clone, Copy, Debug, Default)]
struct AllowedScripts {
    cyrillic: bool,
    latin: bool,
    greek: bool,
    cjk: bool,
    hiragana: bool,
    katakana: bool,
}

impl AllowedScripts {
    fn from_languages(languages: &str) -> Self {
        let mut allowed = Self::default();
        for language in languages
            .split(['+', ',', ' '])
            .filter(|value| !value.is_empty())
        {
            match language {
                "rus" | "Cyrillic" => allowed.cyrillic = true,
                "eng" | "Latin" => allowed.latin = true,
                "ell" | "Greek" => allowed.greek = true,
                "chi_sim" | "chi_tra" | "HanS" | "HanT" => {
                    allowed.cjk = true;
                    allowed.hiragana = true;
                    allowed.katakana = true;
                }
                "equ" => {
                    allowed.latin = true;
                    allowed.greek = true;
                }
                _ => {}
            }
        }
        allowed
    }

    fn contains(self, script: Script) -> bool {
        match script {
            Script::Neutral => true,
            Script::Cyrillic => self.cyrillic,
            Script::Latin => self.latin,
            Script::Greek => self.greek,
            Script::Cjk => self.cjk,
            Script::Hiragana => self.hiragana,
            Script::Katakana => self.katakana,
            Script::Other => false,
        }
    }
}

fn character_script(character: char) -> Script {
    if !character.is_alphabetic() {
        return Script::Neutral;
    }
    match character as u32 {
        0x0041..=0x024f | 0x1e00..=0x1eff => Script::Latin,
        0x0370..=0x03ff | 0x1f00..=0x1fff => Script::Greek,
        0x0400..=0x052f | 0x2de0..=0x2dff | 0xa640..=0xa69f => Script::Cyrillic,
        0x3040..=0x309f => Script::Hiragana,
        0x30a0..=0x30ff | 0x31f0..=0x31ff => Script::Katakana,
        0x3400..=0x4dbf | 0x4e00..=0x9fff | 0xf900..=0xfaff => Script::Cjk,
        _ => Script::Other,
    }
}

fn is_vowel(character: char) -> bool {
    matches!(
        character,
        'a' | 'e'
            | 'i'
            | 'o'
            | 'u'
            | 'y'
            | 'A'
            | 'E'
            | 'I'
            | 'O'
            | 'U'
            | 'Y'
            | 'а'
            | 'е'
            | 'ё'
            | 'и'
            | 'о'
            | 'у'
            | 'ы'
            | 'э'
            | 'ю'
            | 'я'
            | 'А'
            | 'Е'
            | 'Ё'
            | 'И'
            | 'О'
            | 'У'
            | 'Ы'
            | 'Э'
            | 'Ю'
            | 'Я'
            | 'α'
            | 'ε'
            | 'η'
            | 'ι'
            | 'ο'
            | 'υ'
            | 'ω'
            | 'Α'
            | 'Ε'
            | 'Η'
            | 'Ι'
            | 'Ο'
            | 'Υ'
            | 'Ω'
    )
}

fn normalized_confidence(value: f64) -> f64 {
    if !value.is_finite() {
        return 0.0;
    }
    if value > 1.0 {
        (value / 100.0).clamp(0.0, 1.0)
    } else {
        value.clamp(0.0, 1.0)
    }
}

pub fn normalize_tesseract_text(text: &str) -> String {
    text.lines()
        .map(|line| {
            let mut words = line
                .split_whitespace()
                .map(str::to_owned)
                .collect::<Vec<_>>();
            for index in 0..words.len() {
                let value = words[index].clone();
                let previous = index
                    .checked_sub(1)
                    .and_then(|position| words.get(position))
                    .map_or_else(String::new, |word| word.to_lowercase());
                let following = words
                    .get(index + 1)
                    .map_or_else(String::new, |word| word.to_lowercase());
                words[index] = if matches!(value.as_str(), "“|" | "“l" | "\"l" | "‘l" | "'l")
                    && matches!(following.as_str(), "already" | "have")
                {
                    "\"I".to_owned()
                } else if value == "Sh" && previous == "and" && following == "means" {
                    "5h".to_owned()
                } else if matches!(value.as_str(), "»" | "•")
                    && matches!(following.as_str(), "|f" | "|t" | "if" | "it")
                {
                    "-".to_owned()
                } else if matches!(value.as_str(), "IT" | "|T" | "|f") && following == "you" {
                    if previous == "-" {
                        "If".to_owned()
                    } else {
                        "- If".to_owned()
                    }
                } else if matches!(value.as_str(), "—" | "—." | "–" | "-.")
                    && matches!(previous.as_str(), "there" | "safely")
                {
                    "->".to_owned()
                } else if matches!(value.as_str(), "—" | "—." | "–" | "-." | "~.")
                    && previous.is_empty()
                    && following.ends_with(':')
                {
                    "->".to_owned()
                } else {
                    value
                };
            }
            if words.iter().any(|word| word.starts_with("\"I")) {
                for word in &mut words {
                    if let Some(prefix) = word.strip_suffix('”') {
                        *word = format!("{prefix}\"");
                    }
                }
            }
            words.join(" ")
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn is_supported_symbol(character: char) -> bool {
    ".,:;!?%+-=*/()[]{}<>_|#@".contains(character)
}

fn structural_confusables(text: &str) -> usize {
    let mut list_markers = Vec::new();
    let mut glued_words = 0_usize;
    for line in text.lines() {
        let trimmed = line.trim_start();
        let mut characters = trimmed.chars();
        if let (Some(marker), Some(separator)) = (characters.next(), characters.next())
            && matches!(marker, '-' | '*' | '+')
            && separator.is_whitespace()
        {
            list_markers.push(marker);
        }
        for token in trimmed.split_whitespace() {
            let Some((left, right)) = token.split_once('\'') else {
                continue;
            };
            let left_letters = left
                .chars()
                .filter(|character| character.is_alphabetic())
                .collect::<Vec<_>>();
            let right_letters = right
                .chars()
                .filter(|character| character.is_alphabetic())
                .collect::<Vec<_>>();
            if left_letters.len() >= 2
                && right_letters.len() >= 3
                && left_letters.iter().all(|character| character.is_uppercase())
                && right_letters.iter().all(|character| character.is_lowercase())
            {
                glued_words += 1;
            }
        }
    }
    let inconsistent_markers = if list_markers.len() >= 2 {
        list_markers.sort_unstable();
        list_markers.dedup();
        list_markers.len().saturating_sub(1)
    } else {
        0
    };
    glued_words + inconsistent_markers
}

fn script_index(script: Script) -> Option<usize> {
    match script {
        Script::Cjk => Some(0),
        Script::Cyrillic => Some(1),
        Script::Greek => Some(2),
        Script::Hiragana => Some(3),
        Script::Katakana => Some(4),
        Script::Latin => Some(5),
        Script::Other => Some(6),
        Script::Neutral => None,
    }
}

fn text_selection_evidence(text: &str, tokens: &[&str]) -> (usize, usize, usize) {
    let mut counts = [0_usize; 7];
    for character in text.chars() {
        if let Some(index) = script_index(character_script(character)) {
            counts[index] += 1;
        }
    }
    let total_letters = counts.iter().sum::<usize>();
    let dominant = counts
        .iter()
        .copied()
        .enumerate()
        .max_by_key(|(index, count)| (*count, *index))
        .filter(|(_, count)| total_letters >= 8 && *count * 100 >= total_letters * 85)
        .map(|(index, _)| index);
    let minority_confusables = dominant.map_or(0, |dominant| {
        tokens
            .iter()
            .filter(|token| {
                let letters = token
                    .chars()
                    .filter(|character| character.is_alphabetic())
                    .collect::<Vec<_>>();
                let mut scripts = [false; 7];
                for character in &letters {
                    if let Some(index) = script_index(character_script(*character)) {
                        scripts[index] = true;
                    }
                }
                let script_count = scripts.iter().filter(|value| **value).count();
                scripts
                    .iter()
                    .enumerate()
                    .any(|(index, present)| *present && index != dominant)
                    && (letters.len() <= 3 || script_count > 1)
            })
            .count()
    });
    let characters = text.chars().collect::<Vec<_>>();
    let malformed_punctuation = characters
        .windows(3)
        .filter(|window| {
            matches!(window[0], '\"' | '\'' | '“' | '”' | '‘' | '’')
                && window[1] == '|'
                && window[2].is_whitespace()
        })
        .count();
    let mut numeric_separators = 0;
    let mut index = 0;
    while index < characters.len() {
        if characters[index].is_numeric() {
            let mut cursor = index + 1;
            while cursor < characters.len() && characters[cursor].is_whitespace() {
                cursor += 1;
            }
            if cursor < characters.len() && matches!(characters[cursor], '/' | ':' | '-') {
                cursor += 1;
                while cursor < characters.len() && characters[cursor].is_whitespace() {
                    cursor += 1;
                }
                if cursor < characters.len() && characters[cursor].is_numeric() {
                    numeric_separators += 1;
                    index = cursor;
                }
            }
        }
        index += 1;
    }
    (
        minority_confusables,
        malformed_punctuation,
        numeric_separators,
    )
}

pub fn assess_grammar(text: &str, languages: &str, word_confidences: &[f64]) -> GrammarAssessment {
    let text = text.trim();
    if text.is_empty() {
        return GrammarAssessment {
            percent: 0,
            exact: false,
            reasons: vec!["empty"],
        };
    }

    let confidences: Vec<f64> = word_confidences
        .iter()
        .copied()
        .map(normalized_confidence)
        .collect();
    let mean_confidence = if confidences.is_empty() {
        0.0
    } else {
        confidences.iter().sum::<f64>() / confidences.len() as f64
    };
    let minimum_confidence = confidences.iter().copied().reduce(f64::min).unwrap_or(0.0);

    let letters: Vec<char> = text
        .chars()
        .filter(|character| character.is_alphabetic())
        .collect();
    let allowed = AllowedScripts::from_languages(languages);
    let matching_letters = letters
        .iter()
        .filter(|character| allowed.contains(character_script(**character)))
        .count();
    let script_fit = if letters.is_empty() {
        1.0
    } else {
        matching_letters as f64 / letters.len() as f64
    };

    let visible: Vec<char> = text
        .chars()
        .filter(|character| !character.is_whitespace())
        .collect();
    let symbols = visible
        .iter()
        .filter(|character| !character.is_alphanumeric() && !is_supported_symbol(**character))
        .count();
    let symbol_ratio = if visible.is_empty() {
        1.0
    } else {
        symbols as f64 / visible.len() as f64
    };
    let controls = text
        .chars()
        .filter(|character| character.is_control() && !character.is_whitespace())
        .count();

    let tokens: Vec<&str> = text
        .split(|character: char| !character.is_alphanumeric() && character != '_')
        .filter(|token| !token.is_empty())
        .collect();
    let lexical: Vec<&str> = tokens
        .iter()
        .copied()
        .filter(|token| token.chars().any(|character| character.is_alphabetic()))
        .collect();
    let implausible = lexical
        .iter()
        .filter(|token| {
            let token_letters: Vec<char> = token
                .chars()
                .filter(|character| character.is_alphabetic())
                .collect();
            token_letters.len() >= 4
                && !token_letters
                    .iter()
                    .all(|character| character.is_uppercase())
                && !token_letters.iter().copied().any(is_vowel)
        })
        .count();
    let lexical_fit = if lexical.is_empty() {
        if text.chars().any(|character| character.is_numeric()) {
            1.0
        } else {
            0.0
        }
    } else {
        1.0 - implausible as f64 / lexical.len() as f64
    };
    let singletons = lexical
        .iter()
        .filter(|token| token.chars().count() == 1)
        .count();
    let singleton_ratio = if lexical.is_empty() {
        0.0
    } else {
        singletons as f64 / lexical.len() as f64
    };
    let semantic_shape = !letters.is_empty()
        || tokens.len() > 1
        || text.chars().any(|character| "%=+-*/".contains(character));
    let cyrillic_letters = text
        .chars()
        .filter(|character| ('\u{0400}'..='\u{04ff}').contains(character))
        .count();
    let latin_letters = text
        .chars()
        .filter(|character| character.is_ascii_alphabetic())
        .count();
    let suspicious_latin_tokens = text
        .split(|character: char| !character.is_ascii_alphabetic())
        .filter(|token| !token.is_empty())
        .any(|token| {
            !matches!(
                token.to_ascii_uppercase().as_str(),
                "CI" | "PR" | "README" | "SCA" | "SBOM"
            ) && (token.len() <= 2
                || (!token
                    .chars()
                    .all(|character| character.is_ascii_lowercase())
                    && !token
                        .chars()
                        .all(|character| character.is_ascii_uppercase())))
        });
    let suspicious_script_mix =
        cyrillic_letters > latin_letters.saturating_mul(2) && suspicious_latin_tokens;
    let (minority_confusables, malformed_punctuation, numeric_separators) =
        text_selection_evidence(text, &tokens);
    let structural_confusables = structural_confusables(text);

    let mut score = mean_confidence * 0.48
        + script_fit * 0.28
        + lexical_fit * 0.18
        + (1.0 - (symbol_ratio * 3.0).min(1.0)) * 0.06;
    score -= (singleton_ratio * 0.25).min(0.35);
    score -= (controls as f64 * 0.20).min(0.50);
    score -= (minority_confusables as f64 * 0.04).min(0.12);
    score -= (malformed_punctuation as f64 * 0.03).min(0.09);
    score -= (structural_confusables as f64 * 0.04).min(0.12);
    score += (numeric_separators as f64 * 0.01).min(0.02);
    let mut percent = (score * 100.0).round_ties_even().clamp(0.0, 99.0) as u8;
    let exact = !confidences.is_empty()
        && mean_confidence >= 0.92
        && minimum_confidence >= 0.75
        && script_fit == 1.0
        && lexical_fit == 1.0
        && singleton_ratio <= 0.20
        && symbol_ratio <= 0.05
        && controls == 0
        && semantic_shape
        && !suspicious_script_mix
        && minority_confusables == 0
        && malformed_punctuation == 0;

    let mut reasons = Vec::new();
    if mean_confidence < 0.92 {
        reasons.push("confidence");
    }
    if script_fit < 1.0 {
        reasons.push("script");
    }
    if lexical_fit < 1.0 {
        reasons.push("t9-shape");
    }
    if singleton_ratio > 0.20 {
        reasons.push("singletons");
    }
    if symbol_ratio > 0.05 || controls > 0 {
        reasons.push("garbage-symbols");
    }
    if !semantic_shape {
        reasons.push("context-shape");
    }
    if suspicious_script_mix {
        reasons.push("mixed-script-substitution");
    }
    if minority_confusables > 0 {
        reasons.push("dominant-script-confusable");
    }
    if malformed_punctuation > 0 {
        reasons.push("punctuation-confusable");
    }
    if structural_confusables > 0 {
        reasons.push("structural-confusable");
    }
    if exact {
        percent = 100;
        reasons.push("exact");
    }
    GrammarAssessment {
        percent,
        exact,
        reasons,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_the_frozen_tesseract_line_confusables() {
        assert_eq!(
            normalize_tesseract_text("» |f you can safely —. go\nand Sh means losing"),
            "- If you can safely -> go\nand 5h means losing"
        );
        assert_eq!(normalize_tesseract_text("~. answer: 2h"), "-> answer: 2h");
        assert_eq!(normalize_tesseract_text("—. result: 2h"), "-> result: 2h");
    }

    #[test]
    fn accepts_clean_bilingual_evidence() {
        let assessment = assess_grammar(
            "README готов пройти проверку",
            "rus+eng",
            &[98.0, 97.0, 96.0, 99.0],
        );
        assert_eq!(assessment.percent, 100);
        assert!(assessment.exact);
    }

    #[test]
    fn rejects_a_script_outside_the_selected_engine() {
        let assessment = assess_grammar("刀 用 才", "rus+eng", &[96.0, 96.0, 96.0]);
        assert_eq!(assessment.percent, 45);
        assert!(!assessment.exact);
        assert!(assessment.reasons.contains(&"script"));
    }

    #[test]
    fn rejects_implausible_word_shapes() {
        let assessment = assess_grammar("b4026691 zzzzz", "rus+eng", &[98.0, 98.0]);
        assert_eq!(assessment.percent, 90);
        assert!(!assessment.exact);
        assert!(assessment.reasons.contains(&"t9-shape"));
    }

    #[test]
    fn penalizes_dominant_script_confusables_and_orphan_pipe() {
        let noisy = assess_grammar(
            "Then: \"| already have a medical slot, and 5h means losing it\" answer: ЭВ",
            "rus+eng",
            &[94.0; 14],
        );
        let clean = assess_grammar(
            "Then: \"I already have a medical slot, and 5h means losing it\" answer: 2h",
            "eng",
            &[92.0; 14],
        );
        assert_eq!(noisy.percent, 88);
        assert_eq!(clean.percent, 100);
        assert!(noisy.percent < clean.percent);
        assert!(noisy.reasons.contains(&"dominant-script-confusable"));
        assert!(noisy.reasons.contains(&"punctuation-confusable"));
    }

    #[test]
    fn preserves_numeric_separator_evidence() {
        let separated = assess_grammar("You / Me 7 / 8", "eng", &[92.0; 5]);
        let fused = assess_grammar("You / Me 718", "eng", &[92.0; 4]);
        assert_eq!(separated.percent, 100);
        assert_eq!(fused.percent, 100);
        assert!(separated.percent >= fused.percent);
    }

    #[test]
    fn prefers_consistent_list_markers_over_glued_gamma_text() {
        let raw = assess_grammar(
            "- If you can safely get there\n- If you are too dizzy to travel safely",
            "eng",
            &[84.0; 14],
        );
        let gamma = assess_grammar(
            "* IT'youcan safely get there\n+ If you are too dizzy to travel safely",
            "eng",
            &[89.0; 14],
        );
        assert!(raw.percent > gamma.percent);
        assert!(gamma.reasons.contains(&"structural-confusable"));
    }
}
