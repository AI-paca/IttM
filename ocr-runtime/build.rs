fn main() {
    pkg_config::Config::new()
        .atleast_version("5.0")
        .probe("tesseract")
        .expect(
            "Install Tesseract development headers and pkg-config (libtesseract-dev on Debian)",
        );
}
