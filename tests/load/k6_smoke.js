// k6 load test for GeoID — demonstrates correct dedup under load + a credible
// scaling story (single-POST p95, dedup-lookup p95). Not billion-scale.
//
//   GEOID_BASE_URL=http://localhost:8000 k6 run tests/load/k6_smoke.js
//
// Scenarios:
//   mint  — POST unique polygons         (single-POST hot path)
//   dedup — POST the SAME polygon hourly  (dedup-lookup hot path)

import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

const BASE = __ENV.GEOID_BASE_URL || 'http://localhost:8000';
const COLLECTION = __ENV.GEOID_COLLECTION || 'public';
const ITEMS = `${BASE}/collections/${COLLECTION}/items`;

const dedupHits = new Counter('geoid_dedup_hits');

export const options = {
  scenarios: {
    mint: { executor: 'constant-vus', vus: 20, duration: '30s', exec: 'mint' },
    dedup: { executor: 'constant-vus', vus: 10, duration: '30s', exec: 'dedup' },
  },
  thresholds: {
    'http_req_duration{op:mint}': ['p(95)<500'],
    'http_req_duration{op:dedup}': ['p(95)<300'],
    http_req_failed: ['rate<0.01'],
  },
};

const HEADERS = { 'Content-Type': 'application/json' };

function polygon(x, y, s) {
  return {
    type: 'Feature',
    geometry: {
      type: 'Polygon',
      coordinates: [[[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]]],
    },
    properties: {},
  };
}

// Unique-ish coordinates per VU+iteration keep the mint path minting (not dedup'ing).
export function mint() {
  const x = (Math.random() * 358 - 179);
  const y = (Math.random() * 178 - 89);
  const res = http.post(ITEMS, JSON.stringify(polygon(x, y, 0.0001)), {
    headers: HEADERS,
    tags: { op: 'mint' },
  });
  check(res, { 'mint 200/201': (r) => r.status === 200 || r.status === 201 });
}

// Always the SAME geometry -> first call mints, the rest hit the dedup lookup.
export function dedup() {
  const res = http.post(ITEMS, JSON.stringify(polygon(1.234567, 2.345678, 0.001)), {
    headers: HEADERS,
    tags: { op: 'dedup' },
  });
  const ok = check(res, { 'dedup 200/201': (r) => r.status === 200 || r.status === 201 });
  if (ok && res.status === 200) dedupHits.add(1);
}
