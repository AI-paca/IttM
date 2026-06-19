import type { BrowserLayoutPipelineConfig } from "./layout-contracts";
import {
  collectBrowserLayoutFeatures,
  type LayoutAnalysisRaster,
} from "./layout-features";
import { selectBrowserLayout } from "./layout-selectors";
import { executeBrowserLayout } from "./layout-stages";

export function planBrowserLayoutRegions(
  raster: LayoutAnalysisRaster,
  config: BrowserLayoutPipelineConfig,
) {
  const features = collectBrowserLayoutFeatures(
    raster,
    config.featureExtractors,
  );
  const decision = selectBrowserLayout(features, config);
  return {
    features,
    decision,
    regions: executeBrowserLayout(features, decision),
  };
}
