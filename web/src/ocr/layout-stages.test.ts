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
  assert.equal(regions.filter((region) => region.sourceY > 0).length, 4);
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
