// Real capacity and SLO measurement for the tracking API.
//
// The Python benchmark alongside this file is a regression gate — it runs a
// Python load generator against a Python server on the same cores and is itself
// the limiting factor, so its throughput numbers describe the harness. This
// script is the one that produces figures worth quoting to an ad network,
// because k6 is compiled, does not contend for the GIL, and can be run from a
// machine that is not the one under test.
//
// Run it that way. A load generator sharing CPU with the server measures
// contention, not capacity.
//
//   k6 run -e BASE_URL=https://track.example.com -e API_KEY=mmp_live_... \
//          infra/load/ingest.k6.js
//
// The thresholds below are the published SLO from the build plan. If they fail,
// the number in the contract is wrong or the deployment is undersized — and it
// is much cheaper to learn that here than from a customer.

import http from 'k6/http';
import { check } from 'k6';
import { randomString } from 'https://jslib.k6.io/k6-utils/1.4.0/index.js';

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8001';
const API_KEY = __ENV.API_KEY;
const BATCH_SIZE = parseInt(__ENV.BATCH_SIZE || '20', 10);
const TRACKING_CODE = __ENV.TRACKING_CODE;

const ANDROID_UA =
  'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/120 Mobile';

export const options = {
  scenarios: {
    // The redirect gets its own scenario and its own, tighter threshold. It is
    // the only endpoint whose latency is experienced by an advertiser's
    // customers rather than by the advertiser, and the only one where being
    // slow costs conversions directly: a person waiting on a store page leaves.
    redirect: {
      executor: 'ramping-arrival-rate',
      exec: 'redirect',
      startRate: 100,
      timeUnit: '1s',
      preAllocatedVUs: 50,
      maxVUs: 500,
      stages: [
        { target: 1000, duration: '30s' },
        { target: 3000, duration: '1m' },
        { target: 3000, duration: '2m' },
        { target: 0, duration: '30s' },
      ],
    },
    // Ramp to a sustained rate rather than an open flood: arrival-rate load
    // reveals the point where latency degrades, which is the number that
    // matters for capacity planning. A fixed number of VUs just measures
    // whatever the server happens to allow.
    sustained: {
      executor: 'ramping-arrival-rate',
      startRate: 100,
      timeUnit: '1s',
      preAllocatedVUs: 50,
      maxVUs: 500,
      stages: [
        { target: 500, duration: '30s' },
        { target: 2000, duration: '1m' },
        { target: 2000, duration: '2m' },
        { target: 0, duration: '30s' },
      ],
    },
  },
  thresholds: {
    // The published SLO. Stated in the integration docs, so it is a promise.
    'http_req_duration{endpoint:ingest}': ['p(99)<120', 'p(95)<60'],
    // Tighter, because this one is in front of a person who is waiting.
    'http_req_duration{endpoint:redirect}': ['p(99)<80', 'p(95)<40'],
    // A redirect that fails is a conversion that never happens. There is no
    // retry: the person is already gone.
    'http_req_failed{endpoint:redirect}': ['rate<0.0001'],
    // Ingestion returning errors under load is worse than being slow: the SDK
    // will retry, and a retry storm on a struggling service is how a brownout
    // becomes an outage.
    'http_req_failed': ['rate<0.001'],
  },
};

export default function () {
  if (!API_KEY) {
    throw new Error('set API_KEY, e.g. -e API_KEY=mmp_live_...');
  }

  const events = [];
  for (let i = 0; i < BATCH_SIZE; i++) {
    events.push({
      event_id: uuidv7(),
      event_name: 'app_open',
      anonymous_id: `device-${randomString(12)}`,
      platform: 'android',
      app_version: '1.4.2',
      occurred_at: new Date().toISOString(),
    });
  }

  const response = http.post(`${BASE_URL}/v1/events`, JSON.stringify({ events }), {
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${API_KEY}`,
    },
    tags: { endpoint: 'ingest' },
  });

  check(response, {
    'accepted': (r) => r.status === 202,
    'not rate limited': (r) => r.status !== 429,
  });
}

export function redirect() {
  if (!TRACKING_CODE) {
    throw new Error('set TRACKING_CODE, e.g. -e TRACKING_CODE=abc123...');
  }

  const response = http.get(`${BASE_URL}/c/${TRACKING_CODE}`, {
    headers: { 'User-Agent': ANDROID_UA },
    // Do not follow: the store is not ours to load-test, and following would
    // measure Google's latency rather than ours.
    redirects: 0,
    tags: { endpoint: 'redirect' },
  });

  check(response, {
    'redirected': (r) => r.status === 302,
    'carries a click id': (r) =>
      (r.headers['Location'] || '').includes('utm_content'),
  });
}

// UUIDv7, matching the server's format so the ids sort by mint time and land in
// the index the same way production traffic does. Generating v4 here would make
// the benchmark's write pattern unrepresentative of the real one — which is the
// whole reason the server uses v7.
function uuidv7() {
  const ms = Date.now();
  const bytes = new Uint8Array(16);
  for (let i = 0; i < 6; i++) {
    bytes[5 - i] = (ms / Math.pow(2, 8 * i)) & 0xff;
  }
  for (let i = 6; i < 16; i++) {
    bytes[i] = Math.floor(Math.random() * 256);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x70; // version 7
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // RFC 9562 variant
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
