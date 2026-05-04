function init() {
    const form = document.getElementById('upload-form');
    const fileInput = document.getElementById('file-input');
    const btnSubmit = document.getElementById('btn-submit');
    const dropZone = document.getElementById('drop-zone');
    const btnProbe = document.getElementById('btn-probe');
    const btnCopy = document.getElementById('btn-copy');
    const strategyInputs = document.querySelectorAll('input[name="strategy"]');

    let selectedFile = null;

    // Strategy Toggle UI
    strategyInputs.forEach(input => {
        input.addEventListener('change', (e) => {
            document.querySelectorAll('.toggle').forEach(t => t.classList.remove('active'));
            e.target.closest('.toggle').classList.add('active');
        });
    });

    // File Input
    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            on_file_selected(e.target.files[0]);
        }
    });

    // Drag and Drop
    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('dragover');
    });

    dropZone.addEventListener('dragleave', () => {
        dropZone.classList.remove('dragover');
    });

    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('dragover');
        if (e.dataTransfer.files.length > 0) {
            fileInput.files = e.dataTransfer.files;
            on_file_selected(e.dataTransfer.files[0]);
        }
    });

    // Submit Action
    form.addEventListener('submit', (e) => {
        e.preventDefault();
        if (selectedFile) {
            const strategy = document.querySelector('input[name="strategy"]:checked').value;
            if (strategy === 'auto') {
                execute_conversion_with_fallback(selectedFile);
            } else {
                execute_conversion_specific(selectedFile, strategy);
            }
        }
    });

    btnProbe.addEventListener('click', run_probe);
    btnCopy.addEventListener('click', copy_result);

    function on_file_selected(file) {
        selectedFile = file;
        dropZone.querySelector('.drop-text').textContent = file.name;
        dropZone.querySelector('.drop-subtext').textContent = (file.size / 1024 / 1024).toFixed(2) + " MB";
        btnSubmit.disabled = false;
    }
}

async function execute_conversion_specific(file, strategy) {
    const btnSubmit = document.getElementById('btn-submit');
    btnSubmit.disabled = true;
    btnSubmit.textContent = 'Processing...';

    document.getElementById('result-section').style.display = 'none';

    try {
        if (strategy === 'browser') {
            const { run_browser_ocr_low_memory } = await import('./browser-ocr.js');
            const result = await run_browser_ocr_low_memory(file, show_progress);
            render_result(result.markdown, result.meta);
        } else if (strategy === 'local_tesseract' || strategy === 'local_easyocr') {
            // Local Python backend with specific engine
            // Python backend has /v1 prefix for convert endpoint
            let backendUrl = 'http://127.0.0.1:8000/v1/convert';
            let engineType = strategy === 'local_tesseract' ? 'tesseract' : 'easyocr';
            await run_backend_ocr_with_engine(file, backendUrl, engineType);
        } else {
            // Gateway API or auto
            let backendUrl = '/api/convert';
            if (strategy === 'gateway') {
                backendUrl = '/api/convert';
            }
            await run_backend_ocr(file, backendUrl);
        }
    } catch (err) {
        render_error(err.message);
    } finally {
        btnSubmit.disabled = false;
        btnSubmit.textContent = 'Start Intelligent Extraction';
        hide_progress();
    }
}

async function run_backend_ocr_with_engine(file, url, engineType) {
    const formData = new FormData();
    formData.append('file', file);
    
    show_progress(`Processing with ${engineType}...`, 50);

    const fullUrl = new URL(url, window.location.origin);
    fullUrl.searchParams.append('engine_type', engineType);

    const response = await fetch(fullUrl.toString(), {
        method: 'POST',
        body: formData
    });
    
    if (!response.ok) {
        throw new Error(`Server error: ${response.status} ${response.statusText}`);
    }
    
    const data = await response.json();
    if (data.error) throw new Error(data.error);

    render_result(data.markdown, data.meta);
}

async function execute_conversion_with_fallback(file) {
    const btnSubmit = document.getElementById('btn-submit');
    btnSubmit.disabled = true;
    btnSubmit.textContent = 'Processing...';

    document.getElementById('result-section').style.display = 'none';

    try {
        // Try the backend Gateway API first
        console.log("Attempting Gateway API...");
        try {
            await run_backend_ocr(file, '/api/convert');
        } catch (gatewayErr) {
            console.log("Gateway failed, falling back to Browser API...", gatewayErr.message);
            // Dynamic import of browser OCR logic
            const { run_browser_ocr_low_memory } = await import('./browser-ocr.js');
            const result = await run_browser_ocr_low_memory(file, show_progress);
            render_result(result.markdown, result.meta);
        }
    } catch (err) {
        render_error(err.message);
    } finally {
        btnSubmit.disabled = false;
        btnSubmit.textContent = 'Start Intelligent Extraction';
        hide_progress();
    }
}

async function run_backend_ocr(file, url) {
    const formData = new FormData();
    formData.append('file', file);
    
    show_progress("Uploading to backend...", 50);

    const response = await fetch(url, {
        method: 'POST',
        body: formData
    });
    
    if (!response.ok) {
        throw new Error(`Server error: ${response.status} ${response.statusText}`);
    }
    
    const data = await response.json();
    if (data.error) throw new Error(data.error);

    render_result(data.markdown, data.meta);
}

function show_progress(text, percent) {
    const pContainer = document.getElementById('progress-container');
    pContainer.style.display = 'block';
    document.getElementById('progress-text').textContent = text;
    document.getElementById('progress-percent').textContent = parseInt(percent) + "%";
    document.getElementById('progress-fill').style.width = percent + "%";
}

function hide_progress() {
    document.getElementById('progress-container').style.display = 'none';
}

async function run_probe() {
    const btnProbe = document.getElementById('btn-probe');
    const originalText = btnProbe.textContent;
    btnProbe.disabled = true;
    btnProbe.textContent = 'Probing Diagnostics...';
    
    let report = {
        environment: "Unknown",
        browser_capabilities: {
            tesseract_js: typeof Tesseract !== 'undefined' ? "Available" : "Missing",
            memory: navigator.deviceMemory ? navigator.deviceMemory + " GB" : "Unknown",
            webgl: typeof can_use_webgl === 'function' ? (can_use_webgl() ? "Yes" : "No") : "Unknown"
        },
        gateway_api: "Unreachable / 404",
        local_python_direct: "Unreachable (CORS or stopped)"
    };

    try {
        try {
            const gwHealth = await fetch('/api/health');
            if (gwHealth.ok) {
                const gwData = await gwHealth.json();
                report.gateway_api = "OK (Python backend connected via Gateway)";
                report.environment = "Full Stack Server (Node/Bun + Python)";
            } else {
                report.gateway_api = "Error " + gwHealth.status;
                report.environment = "Static Hosting / GitHub Pages";
            }
        } catch(e) {
            report.gateway_api = "Network Error (Probably Static Hosting)";
            report.environment = "Static Hosting / GitHub Pages";
        }

        try {
            const localHealth = await fetch('http://127.0.0.1:8000/health');
            if (localHealth.ok) {
                report.local_python_direct = "OK";
                report.environment = "Local Desktop Environment";
            }
        } catch(e) { }

        const probeOutput = document.getElementById('probe-output');
        probeOutput.textContent = JSON.stringify(report, null, 2);
        document.getElementById('probe-results').style.display = 'block';

        if (report.environment === "Static Hosting / GitHub Pages") {
            console.log("Detected static environment! Will automatically fallback to 'Browser Engine' strategy on extract.");
        }

    } catch (err) {
        alert("Diagnostics core failed: " + err.message);
    } finally {
        btnProbe.disabled = false;
        btnProbe.textContent = originalText;
    }
}

function render_result(markdown, meta) {
    document.getElementById('result-section').style.display = 'block';
    document.getElementById('markdown-output').textContent = markdown;
    if (meta) {
        document.getElementById('meta-info').textContent = 
            `Engine: ${meta.engine} | Chunks: ${meta.chunks} | Time: ${meta.elapsed_ms}ms`;
    }
}

function render_error(message) {
    alert("Error: " + message);
}

function copy_result() {
    const text = document.getElementById('markdown-output').textContent;
    navigator.clipboard.writeText(text).then(() => {
        const btn = document.getElementById('btn-copy');
        const orig = btn.innerHTML;
        btn.innerHTML = '<span>Copied!</span>';
        setTimeout(() => btn.innerHTML = orig, 2000);
    });
}

document.addEventListener('DOMContentLoaded', init);
