import assert from "node:assert/strict";
import test from "node:test";
import type {
  ComponentFeature,
  LayoutDecision,
  LayoutFeatures,
  SeparatorCandidate,
} from "./layout-contracts";
import { executeBrowserLayout } from "./layout-stages";

const decision: LayoutDecision = {
  label: "spatial",
  confidence: 1,
  stages: [
    {
      name: "spatial_regions",
      parameters: {
        maxRegionHeight: 600,
        minRegionHeight: 200,
        minRegionWidth: 80,
        minSeparatorCoverage: 0.55,
      },
    },
  ],
};

function repeatedLayout(
  columns: number,
  components: ComponentFeature[] = [],
): LayoutFeatures {
  const width = 1200;
  const height = 1200;
  const separators: SeparatorCandidate[] = [
    {
      axis: "y",
      start: 595,
      end: 605,
      spanStart: 0,
      spanEnd: width,
      kind: "whitespace",
      strength: 1,
    },
  ];
  for (let column = 1; column < columns; column += 1) {
    const center = (width * column) / columns;
    for (const [spanStart, spanEnd] of [
      [0, 600],
      [600, 1200],
    ]) {
      separators.push({
        axis: "x",
        start: center - 12,
        end: center + 12,
        spanStart,
        spanEnd,
        kind: "whitespace",
        strength: 1,
      });
    }
  }
  return {
    width,
    height,
    foregroundRatio: 0.1,
    separators,
    components,
    scalars: {},
  };
}

for (const columns of [1, 2, 3, 6]) {
  test(`spatial stage preserves ${columns}-column layouts`, () => {
    const regions = executeBrowserLayout(repeatedLayout(columns), decision);

    assert.equal(regions.length, columns * 2);
    assert.equal(
      regions.reduce(
        (area, region) => area + region.sourceWidth * region.sourceHeight,
        0,
      ),
      1200 * 1200,
    );
    assert.ok(
      regions.every(
        (region) =>
          region.sourceWidth > 0 &&
          region.sourceHeight > 0 &&
          region.sourceHeight <= 600,
      ),
    );
  });
}

test("a full-width component blocks an imaginary vertical cut", () => {
  const regions = executeBrowserLayout(
    repeatedLayout(2, [
      {
        bbox: [100, 80, 1100, 520],
        area: 440_000,
        fillRatio: 0.2,
      },
    ]),
    decision,
  );

  assert.equal(regions.filter((region) => region.sourceY === 0).length, 1);
  assert.equal(regions.length, 4);
  assert.ok(regions.every((region) => region.sourceHeight <= 600));
});

test("one giant word crossing a gutter remains one region", () => {
  const features = repeatedLayout(2, [
    {
      bbox: [300, 100, 900, 500],
      area: 200_000,
      fillRatio: 0.3,
    },
    {
      bbox: [300, 700, 900, 1100],
      area: 200_000,
      fillRatio: 0.3,
    },
  ]);

  assert.equal(executeBrowserLayout(features, decision).length, 2);
});

test("line-pair cuts survive wide merged components", () => {
  const features: LayoutFeatures = {
    width: 1000,
    height: 500,
    foregroundRatio: 0.1,
    separators: [
      {
        axis: "x",
        start: 493,
        end: 495,
        spanStart: 0,
        spanEnd: 500,
        kind: "ink",
        strength: 0.9,
      },
      {
        axis: "x",
        start: 505,
        end: 507,
        spanStart: 0,
        spanEnd: 500,
        kind: "ink",
        strength: 0.9,
      },
    ],
    components: [
      {
        bbox: [200, 40, 800, 460],
        area: 250_000,
        fillRatio: 0.3,
      },
    ],
    scalars: {},
  };
  const regions = executeBrowserLayout(features, {
    ...decision,
    stages: [
      {
        name: "spatial_regions",
        parameters: {
          ...decision.stages[0].parameters,
          maxRegionHeight: 500,
          minRegionWidth: 100,
        },
      },
    ],
  });

  assert.deepEqual(
    regions.map((region) => region.sourceX),
    [0, 500],
  );
});

test("whitespace cuts crossing wide components are rejected", () => {
  const features: LayoutFeatures = {
    width: 1000,
    height: 500,
    foregroundRatio: 0.1,
    separators: [
      {
        axis: "x",
        start: 490,
        end: 510,
        spanStart: 0,
        spanEnd: 500,
        kind: "whitespace",
        strength: 1,
      },
    ],
    components: [
      {
        bbox: [200, 40, 800, 460],
        area: 250_000,
        fillRatio: 0.3,
      },
    ],
    scalars: {},
  };
  const regions = executeBrowserLayout(features, {
    ...decision,
    stages: [
      {
        name: "spatial_regions",
        parameters: {
          ...decision.stages[0].parameters,
          maxRegionHeight: 500,
          minRegionWidth: 100,
        },
      },
    ],
  });

  assert.equal(regions.length, 1);
});

test("low-density grid components do not block whitespace cuts", () => {
  const features: LayoutFeatures = {
    width: 1070,
    height: 825,
    foregroundRatio: 0.14,
    separators: [
      {
        axis: "x",
        start: 442,
        end: 487,
        spanStart: 0,
        spanEnd: 825,
        kind: "whitespace",
        strength: 0.9,
      },
      {
        axis: "y",
        start: 120,
        end: 123,
        spanStart: 0,
        spanEnd: 1070,
        kind: "ink",
        strength: 0.8,
      },
      {
        axis: "y",
        start: 240,
        end: 243,
        spanStart: 0,
        spanEnd: 1070,
        kind: "ink",
        strength: 0.8,
      },
      {
        axis: "y",
        start: 360,
        end: 363,
        spanStart: 0,
        spanEnd: 1070,
        kind: "ink",
        strength: 0.8,
      },
    ],
    components: [
      {
        bbox: [23, 36, 1070, 543],
        area: 60_000,
        fillRatio: 0.014,
      },
      {
        bbox: [23, 726, 1070, 820],
        area: 48_000,
        fillRatio: 0.048,
      },
    ],
    scalars: {},
  };
  const regions = executeBrowserLayout(features, {
    ...decision,
    stages: [
      {
        name: "spatial_regions",
        parameters: {
          ...decision.stages[0].parameters,
          maxRegionHeight: 900,
          minRegionWidth: 85,
        },
      },
    ],
  });

  assert.deepEqual(
    regions.map((region) => region.sourceX),
    [0, 464],
  );
});

test("single text rows stay one paragraph row instead of whitespace columns", () => {
  const features: LayoutFeatures = {
    width: 1072,
    height: 77,
    foregroundRatio: 0.1,
    separators: [
      {
        axis: "x",
        start: 180,
        end: 210,
        spanStart: 0,
        spanEnd: 77,
        kind: "whitespace",
        strength: 1,
      },
      {
        axis: "x",
        start: 480,
        end: 510,
        spanStart: 0,
        spanEnd: 77,
        kind: "whitespace",
        strength: 1,
      },
      {
        axis: "x",
        start: 600,
        end: 630,
        spanStart: 0,
        spanEnd: 77,
        kind: "whitespace",
        strength: 1,
      },
      {
        axis: "x",
        start: 178,
        end: 180,
        spanStart: 0,
        spanEnd: 77,
        kind: "ink",
        strength: 0.7,
      },
      {
        axis: "x",
        start: 210,
        end: 212,
        spanStart: 0,
        spanEnd: 77,
        kind: "ink",
        strength: 0.7,
      },
    ],
    components: [],
    scalars: {},
  };
  const regions = executeBrowserLayout(features, {
    ...decision,
    stages: [
      {
        name: "spatial_regions",
        parameters: {
          ...decision.stages[0].parameters,
          maxRegionHeight: 100,
          minRegionWidth: 80,
        },
      },
    ],
  });

  assert.equal(regions.length, 1);
  assert.deepEqual(regions[0], {
    sourceX: 0,
    sourceY: 0,
    sourceWidth: 1072,
    sourceHeight: 77,
  });
});

test("single timeline gutters stay one column without table evidence", () => {
  const features: LayoutFeatures = {
    width: 1562,
    height: 926,
    foregroundRatio: 0.052,
    separators: [
      {
        axis: "x",
        start: 1050,
        end: 1080,
        spanStart: 0,
        spanEnd: 926,
        kind: "whitespace",
        strength: 0.9,
      },
      {
        axis: "y",
        start: 110,
        end: 114,
        spanStart: 0,
        spanEnd: 1500,
        kind: "ink",
        strength: 0.8,
      },
      {
        axis: "y",
        start: 360,
        end: 364,
        spanStart: 0,
        spanEnd: 1500,
        kind: "ink",
        strength: 0.8,
      },
      {
        axis: "y",
        start: 700,
        end: 704,
        spanStart: 0,
        spanEnd: 1500,
        kind: "ink",
        strength: 0.8,
      },
    ],
    components: [],
    scalars: {},
  };
  const regions = executeBrowserLayout(features, {
    ...decision,
    stages: [
      {
        name: "spatial_regions",
        parameters: {
          ...decision.stages[0].parameters,
          maxRegionHeight: 1000,
          minRegionWidth: 125,
        },
      },
    ],
  });

  assert.equal(regions.length, 1);
  assert.equal(regions[0].sourceWidth, 1562);
});

test("low foreground ink-supported gutters stay one column without table evidence", () => {
  const features: LayoutFeatures = {
    width: 1240,
    height: 1278,
    foregroundRatio: 0.072,
    separators: [
      {
        axis: "x",
        start: 585,
        end: 590,
        spanStart: 0,
        spanEnd: 1278,
        kind: "whitespace",
        strength: 0.9,
      },
      {
        axis: "x",
        start: 580,
        end: 582,
        spanStart: 0,
        spanEnd: 1278,
        kind: "ink",
        strength: 0.8,
      },
    ],
    components: [],
    scalars: {},
  };
  const regions = executeBrowserLayout(features, {
    ...decision,
    stages: [
      {
        name: "spatial_regions",
        parameters: {
          ...decision.stages[0].parameters,
          maxRegionHeight: 1400,
          minRegionWidth: 100,
        },
      },
    ],
  });

  assert.deepEqual(regions, [
    {
      sourceX: 0,
      sourceY: 0,
      sourceWidth: 1240,
      sourceHeight: 1278,
    },
  ]);
});

test("component rows become full-width paragraph bands", () => {
  const features: LayoutFeatures = {
    width: 600,
    height: 360,
    foregroundRatio: 0.12,
    separators: [],
    components: [
      { bbox: [20, 30, 360, 80], area: 8000, fillRatio: 0.35 },
      { bbox: [420, 36, 560, 72], area: 1800, fillRatio: 0.3 },
      { bbox: [20, 140, 330, 190], area: 7000, fillRatio: 0.34 },
      { bbox: [430, 146, 560, 182], area: 1700, fillRatio: 0.3 },
      { bbox: [20, 250, 350, 300], area: 7600, fillRatio: 0.35 },
      { bbox: [430, 256, 560, 292], area: 1700, fillRatio: 0.3 },
    ],
    scalars: {},
  };
  const regions = executeBrowserLayout(features, {
    ...decision,
    stages: [
      {
        name: "spatial_regions",
        parameters: {
          ...decision.stages[0].parameters,
          maxRegionHeight: 500,
          minRegionHeight: 80,
        },
      },
    ],
  });

  assert.deepEqual(
    regions.map((region) => [
      region.sourceX,
      region.sourceY,
      region.sourceWidth,
      region.sourceHeight,
    ]),
    [
      [0, 0, 600, 110],
      [0, 110, 600, 110],
      [0, 220, 600, 140],
    ],
  );
});

test("low foreground component rows become full-width paragraph bands", () => {
  const features: LayoutFeatures = {
    width: 800,
    height: 500,
    foregroundRatio: 0.052,
    separators: [],
    components: [
      { bbox: [40, 40, 320, 65], area: 4200, fillRatio: 0.42 },
      { bbox: [40, 135, 470, 165], area: 6400, fillRatio: 0.39 },
      { bbox: [40, 245, 520, 275], area: 7000, fillRatio: 0.36 },
      { bbox: [40, 355, 390, 385], area: 5200, fillRatio: 0.38 },
    ],
    scalars: {},
  };
  const regions = executeBrowserLayout(features, {
    ...decision,
    stages: [
      {
        name: "spatial_regions",
        parameters: {
          ...decision.stages[0].parameters,
          maxRegionHeight: 700,
          minRegionHeight: 160,
        },
      },
    ],
  });

  assert.deepEqual(
    regions.map((region) => [
      region.sourceX,
      region.sourceY,
      region.sourceWidth,
      region.sourceHeight,
    ]),
    [
      [0, 0, 800, 100],
      [0, 100, 800, 105],
      [0, 205, 800, 110],
      [0, 315, 800, 185],
    ],
  );
});

test("low fill components do not create paragraph bands", () => {
  const features: LayoutFeatures = {
    width: 800,
    height: 500,
    foregroundRatio: 0.052,
    separators: [],
    components: [
      { bbox: [0, 30, 800, 85], area: 500, fillRatio: 0.01 },
      { bbox: [0, 150, 800, 210], area: 600, fillRatio: 0.012 },
      { bbox: [0, 275, 800, 340], area: 650, fillRatio: 0.013 },
      { bbox: [0, 405, 800, 470], area: 650, fillRatio: 0.013 },
    ],
    scalars: {},
  };
  const regions = executeBrowserLayout(features, {
    ...decision,
    stages: [
      {
        name: "spatial_regions",
        parameters: {
          ...decision.stages[0].parameters,
          maxRegionHeight: 700,
          minRegionHeight: 160,
        },
      },
    ],
  });

  assert.deepEqual(regions, [
    {
      sourceX: 0,
      sourceY: 0,
      sourceWidth: 800,
      sourceHeight: 500,
    },
  ]);
});

test("drifting central gutters outrank whitespace inside a card", () => {
  const features: LayoutFeatures = {
    width: 1200,
    height: 600,
    foregroundRatio: 0.1,
    separators: [
      {
        axis: "x",
        start: 540,
        end: 620,
        spanStart: 0,
        spanEnd: 320,
        kind: "whitespace",
        strength: 1,
      },
      {
        axis: "x",
        start: 590,
        end: 670,
        spanStart: 280,
        spanEnd: 600,
        kind: "whitespace",
        strength: 1,
      },
      {
        axis: "x",
        start: 890,
        end: 970,
        spanStart: 0,
        spanEnd: 600,
        kind: "whitespace",
        strength: 1,
      },
    ],
    components: [
      {
        bbox: [700, 0, 1120, 600],
        area: 200_000,
        fillRatio: 0.4,
      },
    ],
    scalars: {},
  };

  const regions = executeBrowserLayout(features, decision);

  assert.equal(regions.length, 2);
  assert.ok(regions[0].sourceWidth >= 580);
  assert.ok(regions[0].sourceWidth <= 630);
});

test("neighbor gutters and line pairs split card grids deterministically", () => {
  const features: LayoutFeatures = {
    width: 1280,
    height: 600,
    foregroundRatio: 0.16,
    separators: [
      {
        axis: "x",
        start: 96,
        end: 233,
        spanStart: 320,
        spanEnd: 600,
        kind: "whitespace",
        strength: 0.95,
      },
      {
        axis: "x",
        start: 201,
        end: 227,
        spanStart: 0,
        spanEnd: 580,
        kind: "whitespace",
        strength: 0.46,
      },
      {
        axis: "x",
        start: 201,
        end: 228,
        spanStart: 0,
        spanEnd: 600,
        kind: "whitespace",
        strength: 0.47,
      },
      {
        axis: "x",
        start: 233,
        end: 235,
        spanStart: 0,
        spanEnd: 600,
        kind: "ink",
        strength: 0.26,
      },
      {
        axis: "x",
        start: 425,
        end: 427,
        spanStart: 0,
        spanEnd: 600,
        kind: "ink",
        strength: 0.26,
      },
      {
        axis: "x",
        start: 427,
        end: 443,
        spanStart: 0,
        spanEnd: 580,
        kind: "whitespace",
        strength: 0.41,
      },
      {
        axis: "x",
        start: 427,
        end: 443,
        spanStart: 300,
        spanEnd: 600,
        kind: "whitespace",
        strength: 0.99,
      },
      {
        axis: "x",
        start: 443,
        end: 445,
        spanStart: 0,
        spanEnd: 600,
        kind: "ink",
        strength: 0.22,
      },
      {
        axis: "x",
        start: 634,
        end: 636,
        spanStart: 0,
        spanEnd: 600,
        kind: "ink",
        strength: 0.22,
      },
      {
        axis: "x",
        start: 652,
        end: 654,
        spanStart: 0,
        spanEnd: 600,
        kind: "ink",
        strength: 0.3,
      },
      {
        axis: "x",
        start: 673,
        end: 675,
        spanStart: 0,
        spanEnd: 600,
        kind: "ink",
        strength: 0.22,
      },
      {
        axis: "x",
        start: 821,
        end: 823,
        spanStart: 0,
        spanEnd: 600,
        kind: "ink",
        strength: 0.22,
      },
      {
        axis: "x",
        start: 843,
        end: 845,
        spanStart: 0,
        spanEnd: 600,
        kind: "ink",
        strength: 0.3,
      },
      {
        axis: "x",
        start: 861,
        end: 863,
        spanStart: 0,
        spanEnd: 600,
        kind: "ink",
        strength: 0.26,
      },
      {
        axis: "x",
        start: 1052,
        end: 1054,
        spanStart: 0,
        spanEnd: 600,
        kind: "ink",
        strength: 0.26,
      },
      {
        axis: "x",
        start: 1054,
        end: 1070,
        spanStart: 0,
        spanEnd: 580,
        kind: "whitespace",
        strength: 0.58,
      },
      {
        axis: "x",
        start: 1054,
        end: 1070,
        spanStart: 300,
        spanEnd: 600,
        kind: "whitespace",
        strength: 1,
      },
      {
        axis: "x",
        start: 1070,
        end: 1072,
        spanStart: 0,
        spanEnd: 600,
        kind: "ink",
        strength: 0.28,
      },
      {
        axis: "x",
        start: 1240,
        end: 1241,
        spanStart: 0,
        spanEnd: 600,
        kind: "ink",
        strength: 0.18,
      },
      {
        axis: "x",
        start: 1261,
        end: 1263,
        spanStart: 0,
        spanEnd: 600,
        kind: "ink",
        strength: 0.28,
      },
    ],
    components: [],
    scalars: {},
  };
  const regions = executeBrowserLayout(features, {
    ...decision,
    stages: [
      {
        name: "spatial_regions",
        parameters: {
          ...decision.stages[0].parameters,
          maxRegionHeight: 600,
          minRegionWidth: 102,
        },
      },
    ],
  });

  assert.deepEqual(
    regions.map((region) => region.sourceX),
    [0, 214, 435, 644, 853, 1062],
  );
  assert.deepEqual(
    regions.map((region) => region.sourceWidth),
    [214, 221, 209, 209, 209, 218],
  );
});

test("a short cross-column footer does not merge a tall card row", () => {
  const features = repeatedLayout(2, [
    {
      bbox: [400, 1120, 800, 1180],
      area: 20_000,
      fillRatio: 0.8,
    },
  ]);
  const tallDecision: LayoutDecision = {
    ...decision,
    stages: [
      {
        name: "spatial_regions",
        parameters: {
          ...decision.stages[0].parameters,
          maxRegionHeight: 1200,
        },
      },
    ],
  };

  const regions = executeBrowserLayout(features, tallDecision);

  assert.equal(regions.length, 2);
});
