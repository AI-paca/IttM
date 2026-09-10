//! Lossless storage for immutable raster planes. The ABI always exposes RGB bytes.
use std::borrow::Cow;
use std::sync::Arc;

#[derive(Clone, Debug, Default)]
pub(crate) struct RasterPixels {
    bytes: Arc<[u8]>,
    length: usize,
    packed: bool,
}

impl From<Vec<u8>> for RasterPixels {
    fn from(pixels: Vec<u8>) -> Self {
        let length = pixels.len();
        if length >= 65_536 {
            let mut encoded = Vec::with_capacity(length / 4);
            let mut at = 0;
            while at < length {
                let mut run = 1;
                while run < 128 && at + run < length && pixels[at + run] == pixels[at] {
                    run += 1;
                }
                if run >= 3 {
                    encoded.extend_from_slice(&[0x80 | (run - 1) as u8, pixels[at]]);
                    at += run;
                } else {
                    let start = at;
                    at += run;
                    while at < length && at - start < 128 {
                        if at + 2 < length && pixels[at] == pixels[at + 1] && pixels[at] == pixels[at + 2] {
                            break;
                        }
                        at += 1;
                    }
                    encoded.push((at - start - 1) as u8);
                    encoded.extend_from_slice(&pixels[start..at]);
                }
            }
            if encoded.len() < length {
                return Self { bytes: encoded.into(), length, packed: true };
            }
        }
        Self { bytes: pixels.into(), length, packed: false }
    }
}

impl RasterPixels {
    pub(crate) fn len(&self) -> usize { self.length }
    pub(crate) fn is_empty(&self) -> bool { self.length == 0 }
    #[allow(dead_code)] // Used by memory diagnostics of the native/WASM transport.
    pub(crate) fn stored_len(&self) -> usize { self.bytes.len() }

    pub(crate) fn copy_to(&self, output: &mut [u8]) {
        assert_eq!(output.len(), self.length);
        if !self.packed {
            output.copy_from_slice(&self.bytes);
            return;
        }
        let (mut source, mut target) = (0, 0);
        while source < self.bytes.len() {
            let control = self.bytes[source];
            let count = usize::from(control & 0x7f) + 1;
            source += 1;
            if control & 0x80 != 0 {
                output[target..target + count].fill(self.bytes[source]);
                source += 1;
            } else {
                output[target..target + count].copy_from_slice(&self.bytes[source..source + count]);
                source += count;
            }
            target += count;
        }
        debug_assert_eq!(target, self.length);
    }

    pub(crate) fn decoded(&self) -> Cow<'_, [u8]> {
        if !self.packed { return Cow::Borrowed(&self.bytes); }
        let mut pixels = vec![0; self.length];
        self.copy_to(&mut pixels);
        Cow::Owned(pixels)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn raster_storage_roundtrips_runs_literals_and_partial_packets() {
        let mut image = Vec::new();
        for value in 0..=255_u8 {
            for length in [1, 2, 3, 127, 128, 129, 255, 256] {
                image.extend(std::iter::repeat_n(value, length));
                image.extend(0..=255_u8);
            }
        }
        let pixels = RasterPixels::from(image.clone());
        assert!(pixels.packed);
        assert_eq!(pixels.decoded().as_ref(), image);
        let mut copied = vec![0; image.len()];
        pixels.copy_to(&mut copied);
        assert_eq!(copied, image);
    }

    #[test]
    fn raster_storage_keeps_uncompressible_and_empty_planes() {
        for image in [Vec::new(), (0..131_073).map(|i| (i % 251) as u8).collect()] {
            let pixels = RasterPixels::from(image.clone());
            assert!(!pixels.packed);
            assert_eq!(pixels.decoded().as_ref(), image);
            assert_eq!(pixels.len(), image.len());
        }
    }

    #[test]
    fn raster_clones_share_lossless_storage() {
        let pixels = RasterPixels::from(vec![255; 3_000_000]);
        let cloned = pixels.clone();
        assert!(Arc::ptr_eq(&pixels.bytes, &cloned.bytes));
        assert!(pixels.stored_len() < pixels.len() / 50);
        assert!(cloned.decoded().iter().all(|v| *v == 255));
    }
}
