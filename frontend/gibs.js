/* NASA GIBS true-color cloud imagery for the globe — a visual complement
 * to the numeric cloud-cover forecast already used for pass visibility.
 * Different kind of data entirely: this is a recent actual satellite
 * photograph (MODIS Terra's daytime pass, roughly a day old depending on
 * processing lag), not a forecast — "here's what clouds actually looked
 * like," not "will it be cloudy Thursday night."
 *
 * getNumberOfXTilesAtLevel/tileXYToRectangle/positionToTileXY below are a
 * direct port of NASA's own reference tiling scheme, not a guess — GIBS
 * serves EPSG:4326 imagery in a custom tile layout Cesium's stock
 * GeographicTilingScheme doesn't match, and getting those formulas wrong
 * would misalign every tile. Ported from:
 * https://github.com/nasa-gibs/gibs-web-examples/blob/master/examples/cesium/gibs.js
 *
 * Copyright 2013 - 2020 United States Government as represented by the
 * Administrator of the National Aeronautics and Space Administration.
 * Licensed under the Apache License, Version 2.0:
 * http://www.apache.org/licenses/LICENSE-2.0
 */

function geographicTilingScheme() {
  const self = new Cesium.GeographicTilingScheme();
  const tilePixels = 512;
  const rectangle = Cesium.Rectangle.MAX_VALUE;

  const levels = [
    { width: 2, height: 1, resolution: 0.009817477042468103 },
    { width: 3, height: 2, resolution: 0.004908738521234052 },
    { width: 5, height: 3, resolution: 0.002454369260617026 },
    { width: 10, height: 5, resolution: 0.001227184630308513 },
    { width: 20, height: 10, resolution: 0.0006135923151542565 },
    { width: 40, height: 20, resolution: 0.00030679615757712823 },
    { width: 80, height: 40, resolution: 0.00015339807878856412 },
    { width: 160, height: 80, resolution: 0.00007669903939428206 },
    { width: 320, height: 160, resolution: 0.00003834951969714103 },
  ];

  self.getNumberOfXTilesAtLevel = (level) => levels[level].width;
  self.getNumberOfYTilesAtLevel = (level) => levels[level].height;

  self.tileXYToRectangle = (x, y, level, result) => {
    const resolution = levels[level].resolution;
    const xTileWidth = resolution * tilePixels;
    const west = x * xTileWidth + rectangle.west;
    const east = (x + 1) * xTileWidth + rectangle.west;
    const yTileHeight = resolution * tilePixels;
    const north = rectangle.north - y * yTileHeight;
    const south = rectangle.north - (y + 1) * yTileHeight;
    if (!result) result = new Cesium.Rectangle(0, 0, 0, 0);
    result.west = west;
    result.south = south;
    result.east = east;
    result.north = north;
    return result;
  };

  self.positionToTileXY = (position, level, result) => {
    if (!Cesium.Rectangle.contains(rectangle, position)) return undefined;
    const xTiles = levels[level].width;
    const yTiles = levels[level].height;
    const resolution = levels[level].resolution;
    const xTileWidth = resolution * tilePixels;
    const yTileHeight = resolution * tilePixels;

    let longitude = position.longitude;
    if (rectangle.east < rectangle.west) longitude += Cesium.Math.TWO_PI;
    let xTileCoordinate = ((longitude - rectangle.west) / xTileWidth) | 0;
    if (xTileCoordinate >= xTiles) xTileCoordinate = xTiles - 1;

    const latitude = position.latitude;
    let yTileCoordinate = ((rectangle.north - latitude) / yTileHeight) | 0;
    if (yTileCoordinate > yTiles) yTileCoordinate = yTiles - 1;

    if (!result) result = new Cesium.Cartesian2(0, 0);
    result.x = xTileCoordinate;
    result.y = yTileCoordinate;
    return result;
  };

  return self;
}

// NASA's near-real-time processing has some lag — shortly after UTC
// midnight, the latest day's mosaic isn't ready yet, so fall back to
// yesterday's. Same logic as NASA's own reference viewer.
function mostRecentAvailableDate() {
  const date = new Date();
  if (date.getUTCHours() < 3) {
    date.setUTCDate(date.getUTCDate() - 1);
  }
  return date.toISOString().split("T")[0];
}

export function createTrueColorCloudsLayer() {
  return new Cesium.WebMapTileServiceImageryProvider({
    url: `https://gibs.earthdata.nasa.gov/wmts/epsg4326/best/wmts.cgi?TIME=${mostRecentAvailableDate()}`,
    layer: "MODIS_Terra_CorrectedReflectance_TrueColor",
    style: "",
    format: "image/jpeg",
    tileMatrixSetID: "250m",
    maximumLevel: 8,
    tileWidth: 512,
    tileHeight: 512,
    tilingScheme: geographicTilingScheme(),
    credit: new Cesium.Credit("NASA EOSDIS GIBS / MODIS Terra"),
  });
}
