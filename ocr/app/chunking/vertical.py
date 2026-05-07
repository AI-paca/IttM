import os
import numpy as np
from PIL import Image


def remove_white_borders(image: Image.Image, bg_threshold: int = 240) -> Image.Image:
    """
    Crops out empty white borders from the image to help horizontal projection
    when there's content on one side and empty space on the other.
    """
    if image.mode != "L":
        gray = image.convert("L")
    else:
        gray = image
    img_arr = np.array(gray)
    mask = img_arr < bg_threshold
    coords = np.argwhere(mask)
    if coords.size == 0:
        return image
    y_min, x_min = coords.min(axis=0)
    y_max, x_max = coords.max(axis=0)
    pad = 10
    x_min = max(0, x_min - pad)
    y_min = max(0, y_min - pad)
    x_max = min(image.size[0], x_max + pad)
    y_max = min(image.size[1], y_max + pad)
    return image.crop((x_min, y_min, x_max, y_max))

def find_blank_horizontal_bands(image: Image.Image, min_gap: int = 10) -> list:
    """
    Finds empty horizontal bands (rows with little to no content) between cards.
    Returns list of (start_y, end_y) tuples representing blank band positions.
    """
    width, height = image.size
    
    # Convert to grayscale array for projection
    gray = image.convert("L")
    img_arr = np.array(gray)
    
    # Otsu thresholding to separate background and text
    hist, bins = np.histogram(img_arr.flatten(), 256, (0, 256))
    p = hist.astype(float) / img_arr.size
    omega = np.cumsum(p)
    mu = np.cumsum(p * np.arange(256))
    mu_t = mu[-1]
    
    # Maximize between-class variance
    with np.errstate(divide='ignore', invalid='ignore'):
        sigma_b_squared = (mu_t * omega - mu)**2 / (omega * (1 - omega))
        
    optimal_threshold = np.nanargmax(sigma_b_squared)
    
    # Text is dark pixels on white background
    binary = img_arr < optimal_threshold
    
    # Horizontal projection: calculate total ink (text) per row
    ink = np.sum(binary, axis=1)
    
    # Consider a row "blank" if very little ink (less than 0.5% of row width)
    blank_rows = ink < (width * 0.005)
    
    # Find continuous blank gaps
    bands = []
    in_gap = False
    gap_start = 0
    
    for i, is_blank in enumerate(blank_rows):
        if is_blank and not in_gap:
            in_gap = True
            gap_start = i
        elif not is_blank and in_gap:
            in_gap = False
            gap_height = i - gap_start
            if gap_height >= min_gap:
                bands.append((gap_start, i))
                
    # Check if image ends with a blank band
    if in_gap:
        gap_height = height - gap_start
        if gap_height >= min_gap:
            bands.append((gap_start, height))
    
    return bands


def split_by_blank_bands(image: Image.Image, min_gap: int = 10) -> list:
    """
    Splits screenshot by empty horizontal places between cards.
    Returns list of image chunks cropped at blank bands.
    """
    width, height = image.size
    bands = find_blank_horizontal_bands(image, min_gap)
    
    if not bands:
        return []
    
    chunks = []
    last_end = 0
    
    for start, end in bands:
        # Cut from last_end to start of blank band
        if start > last_end:
            chunks.append(image.crop((0, last_end, width, start)))
        last_end = end
    
    # Add remaining part after last blank band
    if last_end < height:
        chunks.append(image.crop((0, last_end, width, height)))
    
    return chunks


def fallback_split_with_overlap(image: Image.Image, chunk_height: int = 1600, overlap: int = 120) -> list:
    """
    Fallback: splits image with fixed height and overlap if no blank bands found.
    """
    width, height = image.size
    chunks = []
    y = 0
    
    while y < height:
        box = (0, y, width, min(y + chunk_height, height))
        chunks.append(image.crop(box))
        y += (chunk_height - overlap)
        if y >= height - overlap:
            break
    
    return chunks


def split_vertical(image: Image.Image, chunk_height: int = 1600, overlap: int = 120) -> list:
    """
    Main entry point: splits image vertically using card-aware chunking.
    First tries to find blank horizontal bands between cards.
    Falls back to fixed sizing with overlap if no blank bands found.
    """
    try:
        image = remove_white_borders(image)
        width, height = image.size
        if height <= chunk_height:
            return [image]
        
        # Try card-aware splitting first
        chunks = split_by_blank_bands(image)
        
        # If we found meaningful chunks (more than 1, and each is reasonable size)
        if len(chunks) > 1:
            # Filter out very small chunks (likely noise)
            min_chunk_height = chunk_height // 4
            filtered_chunks = [c for c in chunks if c.size[1] >= min_chunk_height]
            if filtered_chunks:
                return filtered_chunks
        
        # Fallback to overlap-based splitting
        return fallback_split_with_overlap(image, chunk_height, overlap)
        
    except Exception as e:
        print(f"Error in split_vertical: {e}")
        # Fallback simple overlap
        return fallback_split_with_overlap(image, chunk_height, overlap)
