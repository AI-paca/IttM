//! Owned Rust interface to the same stage engine exported through the C/WASM ABI.
//! Native hosts keep pixels and word evidence in Rust; no interpreter is involved.
use crate::{ImagePlane, PixelFormat, language::LanguageProfileId, separated::*};

pub const STAGES: [&str; 8] = [
    "preprocess",
    "geometry",
    "topology",
    "find-object",
    "separate-block",
    "ocr-blocks",
    "get-segment",
    "generate-object",
];

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Job {
    pub index: u32,
    pub fields: [i32; 18],
    pub languages: &'static str,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Word {
    pub text: String,
    pub bbox: [u32; 4],
    pub confidence_ppm: u32,
}

/// A handle has a single owner. Dropping it releases all page rasters and evidence.
pub struct Session(u32);

impl Session {
    pub fn plan(image: &ImagePlane) -> Result<Self, String> {
        if !image.is_valid() {
            return Err("Invalid image plane".into());
        }
        let length = u32::try_from(image.pixels.len()).map_err(|_| "Image exceeds ABI limit")?;
        // SAFETY: ImagePlane validation proves that the complete buffer and stride exist.
        let handle = unsafe {
            ittm_separated_plan_begin(
                image.pixels.as_ptr(),
                length,
                image.width,
                image.height,
                image.stride,
                image.format as u32,
            )
        };
        if handle == 0 {
            return Err("Rust pipeline rejected the image".into());
        }
        Ok(Self(handle))
    }

    pub fn start_ocr(&mut self) -> Result<(), String> {
        if ittm_separated_start_ocr(self.0) == 1 {
            Ok(())
        } else {
            Err("Cannot start OCR at this stage".into())
        }
    }

    pub fn job_count(&self) -> u32 {
        ittm_separated_job_count(self.0)
    }

    pub fn job(&self, index: u32) -> Result<Job, String> {
        if index >= self.job_count() {
            return Err("Unknown OCR job".into());
        }
        let fields =
            std::array::from_fn(|field| ittm_separated_job_field(self.0, index, field as u32));
        let profile = LanguageProfileId::ALL
            .get(fields[11] as usize)
            .ok_or("Unknown language profile")?;
        Ok(Job {
            index,
            fields,
            languages: profile.code(),
        })
    }

    pub fn raster(&self, index: u32) -> Result<ImagePlane, String> {
        self.job(index)?;
        let field = |n| ittm_separated_job_raster_field(self.0, index, n);
        let (width, height, stride) = (field(0), field(1), field(2));
        if width <= 0 || height <= 0 || stride <= 0 || field(3) != 3 {
            return Err("Invalid OCR job raster".into());
        }
        let length = ittm_separated_job_raster_length(self.0, index);
        let mut pixels = vec![0; length as usize];
        // SAFETY: output owns exactly length bytes and the valid session stays alive.
        let copied =
            unsafe { ittm_separated_job_raster_copy(self.0, index, pixels.as_mut_ptr(), length) };
        if copied != length as i32 {
            return Err("Could not copy OCR raster".into());
        }
        Ok(ImagePlane {
            width: width as u32,
            height: height as u32,
            stride: stride as u32,
            format: PixelFormat::Rgb8,
            pixels,
        })
    }

    pub fn submit(&mut self, index: u32, text: &str, words: &[Word]) -> Result<(), String> {
        self.job(index)?;
        let text_length = u32::try_from(text.len()).map_err(|_| "OCR text exceeds ABI limit")?;
        for word in words {
            let length =
                u32::try_from(word.text.len()).map_err(|_| "OCR word exceeds ABI limit")?;
            let [left, top, right, bottom] = word.bbox;
            // SAFETY: the strings remain alive for the synchronous call; the ABI copies them.
            let code = unsafe {
                ittm_separated_add_ocr_word_ppm(
                    self.0,
                    index,
                    word.text.as_ptr(),
                    length,
                    left,
                    top,
                    right,
                    bottom,
                    word.confidence_ppm,
                )
            };
            if code != 0 {
                return Err(format!("Rejected OCR word: {code}"));
            }
        }
        // SAFETY: text_length describes the live UTF-8 slice.
        let code = unsafe {
            ittm_separated_set_ocr(
                self.0,
                index,
                text.as_ptr(),
                text_length,
                if words.is_empty() { 1 } else { 0 },
            )
        };
        if code != 0 {
            return Err(format!("Rejected OCR result: {code}"));
        }
        Ok(())
    }

    pub fn render(&mut self) -> Result<String, String> {
        if self.stage_mask() & (1 << 5) == 0 {
            return Err("OCR is incomplete".into());
        }
        let length = ittm_separated_render_length(self.0);
        let mut output = vec![0; length as usize];
        // SAFETY: output owns capacity bytes and this is the sole owner of the handle.
        let copied = unsafe { ittm_separated_render_copy(self.0, output.as_mut_ptr(), length) };
        if copied != length as i32 {
            return Err("Could not copy rendered text".into());
        }
        String::from_utf8(output).map_err(|error| error.to_string())
    }

    pub fn stage_mask(&self) -> u32 {
        ittm_separated_stage_mask(self.0)
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        ittm_separated_drop(self.0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_page_completes_without_calling_an_engine() {
        let image = ImagePlane {
            width: 40,
            height: 30,
            stride: 120,
            format: PixelFormat::Rgb8,
            pixels: vec![255; 3600],
        };
        let mut session = Session::plan(&image).unwrap();
        assert!(session.render().is_err());
        session.start_ocr().unwrap();
        assert_eq!(session.job_count(), 0);
        assert_eq!(session.render().unwrap(), "");
        assert_eq!(session.stage_mask(), 255);
        assert!(session.raster(0).is_err());
    }

    #[test]
    fn rejects_truncated_image_before_crossing_abi() {
        let image = ImagePlane {
            width: 20,
            height: 30,
            stride: 60,
            format: PixelFormat::Rgb8,
            pixels: vec![0; 10],
        };
        assert!(Session::plan(&image).is_err());
    }
}
