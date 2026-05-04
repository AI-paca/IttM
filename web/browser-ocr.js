// Optimized Low-Memory Browser OCR
export async function run_browser_ocr_low_memory(file, progressCallback) {
    if (!file.type.startsWith('image/')) {
        throw new Error("Browser strategy only supports images.");
    }
    
    progressCallback("Loading image to memory...", 5);
    
    // Use createImageBitmap to avoid heavy DOM Image object if possible, 
    // but we can load it to canvas directly.
    const imgUrl = URL.createObjectURL(file);
    const img = await loadImage(imgUrl);
    
    const CHUNK_HEIGHT = 1500;
    const OVERLAP = 100;
    
    let combinedText = "";
    const startTime = Date.now();
    
    // Use Web Workers via Tesseract.js (already done in lib, but we isolate it)
    const worker = await Tesseract.createWorker("eng+rus+chi_sim", 1, {
        logger: m => {
            if (m.status === 'recognizing text') {
                 let dynamicProgress = Math.round(m.progress * 100);
                 progressCallback(`Recognizing chunk ${window._currentChunk} of ${window._totalChunks} (${dynamicProgress}%)`, m.progress * 100);
            }
        }
    });

    const getChunk = (yStart, h) => {
        // Create canvas only for the current chunk
        const canvas = document.createElement('canvas');
        canvas.width = img.width;
        canvas.height = h;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(img, 0, yStart, img.width, h, 0, 0, img.width, h);
        return canvas;
    }

    let tempY = 0;
    let totalChunks = 0;
    while(tempY < img.height) {
        totalChunks++;
        tempY += (CHUNK_HEIGHT - OVERLAP);
        if (tempY >= img.height - OVERLAP && tempY < img.height) { totalChunks++; break; }
    }
    window._totalChunks = totalChunks;

    let y = 0;
    let chunksCount = 0;

    while (y < img.height) {
        chunksCount++;
        window._currentChunk = chunksCount;
        const h = Math.min(CHUNK_HEIGHT, img.height - y);
        progressCallback(`Extracting chunk ${chunksCount}/${totalChunks}...`, 10);
        
        const chunkData = getChunk(y, h);
        const ret = await worker.recognize(chunkData);
        combinedText += ret.data.text + "\n\n";
        
        // IMMEDIATE MEMORY DEALLOCATION
        chunkData.width = 0;
        chunkData.height = 0;
        
        y += (CHUNK_HEIGHT - OVERLAP);
        if (y >= img.height - OVERLAP && y < img.height) {
             chunksCount++;
             window._currentChunk = chunksCount;
             const remainH = img.height - y;
             const remainData = getChunk(y, remainH);
             const retR = await worker.recognize(remainData);
             combinedText += retR.data.text + "\n\n";
             
             // IMMEDIATE MEMORY DEALLOCATION
             remainData.width = 0;
             remainData.height = 0;
             break;
        }
    }
    
    await worker.terminate();
    URL.revokeObjectURL(imgUrl); // Release image URL
    img.src = ''; // Release image reference
    
    const elapsed = Date.now() - startTime;
    return {
        markdown: combinedText,
        meta: {
            engine: "tesseract.js (browser low-memory)",
            chunks: chunksCount,
            pages: 1,
            elapsed_ms: elapsed
        }
    };
}

function loadImage(url) {
    return new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => resolve(img);
        img.onerror = reject;
        img.src = url;
    });
}
