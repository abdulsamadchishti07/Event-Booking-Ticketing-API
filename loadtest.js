// k6 load test for the Event Booking API.
//   k6 run -e TARGET=http://<server-ip> -e BROWSE_RPS=500 -e BOOK_RPS=50 loadtest.js
// Raise BROWSE_RPS / BOOK_RPS step by step; the last step that passes the thresholds is your capacity.
// Scenarios: "browse" (cached reads + cache misses) and "drop" (many users fight for 500 seats at once).
import http from 'k6/http';
import { check } from 'k6';
import { SharedArray } from 'k6/data';
import { Counter } from 'k6/metrics';
import { textSummary } from 'https://jslib.k6.io/k6-summary/0.0.2/index.js';

const ctx = JSON.parse(open('./loadctx.json'));
const tokens = new SharedArray('tokens', () => ctx.tokens);
const TARGET = __ENV.TARGET || 'http://localhost';
const BROWSE_RPS = parseInt(__ENV.BROWSE_RPS || '200');
const BOOK_RPS = parseInt(__ENV.BOOK_RPS || '20');
const HOLD = __ENV.HOLD || '3m';            // time at full rate
const DROP_USERS = parseInt(__ENV.DROP_USERS || '2000');
const SPOOF_IP = __ENV.SPOOF_IP === '1';

const serverErrors = new Counter('server_errors_5xx');
const seatsWon = new Counter('seats_won_201');
const seatsLost = new Counter('seats_lost_409');
const rateLimited429 = new Counter('rate_limited_429');
const poolExhausted503 = new Counter('pool_exhausted_503');
const internalError500 = new Counter('internal_error_500');

export const options = {
  scenarios: {
    // open model: k6 keeps sending this many requests/second even if the server slows down
    browse: {
      executor: 'ramping-arrival-rate', exec: 'browse',
      startRate: 10, timeUnit: '1s', preAllocatedVUs: 200, maxVUs: 5000,
      stages: [{ target: BROWSE_RPS, duration: '1m' }, { target: BROWSE_RPS, duration: HOLD }, { target: 0, duration: '20s' }],
    },
    // a ticket drop: DROP_USERS users each try once to grab a random seat, all within ~10 seconds
    drop: {
      executor: 'per-vu-iterations', exec: 'drop', vus: DROP_USERS, iterations: 1,
      startTime: '90s', maxDuration: '2m',
    },
    // steady background booking holds on the big general-admission event
    book: {
      executor: 'constant-arrival-rate', exec: 'book', rate: BOOK_RPS, timeUnit: '1s',
      duration: HOLD, startTime: '60s', preAllocatedVUs: 100, maxVUs: 2000,
    },
  },
  thresholds: {
    'http_req_failed': ['rate<0.01'],                         // <1% errors overall
    'http_req_duration{scenario:browse}': ['p(95)<500'],
    'http_req_duration{scenario:book}': ['p(95)<800'],
    'server_errors_5xx': ['count<10'],
  },
};

// Note on Rate Limiting:
// When running k6 from a single runner against Nginx, all virtual users share the runner's IP
// (since Nginx overwrites X-Forwarded-For with $remote_addr). For high-throughput single-runner
// load tests, set BOOKINGS_RATE_LIMIT=100000, SERVICES_RATE_LIMIT=100000, AUTH_RATE_LIMIT=100000
// in your app environment, or distribute k6 across multiple runner machines.
function hdr(i, auth) {
  const h = { 'Content-Type': 'application/json' };
  if (auth) h.Authorization = `Bearer ${tokens[i % tokens.length]}`;
  if (SPOOF_IP) {
    // Generate a unique IP per virtual user (for benchmarking directly against uvicorn)
    const o2 = Math.floor(i / 256) % 256;
    const o3 = i % 256;
    h['X-Forwarded-For'] = `198.51.${o2}.${o3}`;
  }
  return { headers: h };
}

function track(res) {
  if (res.status === 429) rateLimited429.add(1);
  else if (res.status === 503) { poolExhausted503.add(1); serverErrors.add(1); }
  else if (res.status >= 500) { internalError500.add(1); serverErrors.add(1); }
}

export function browse() {
  const r = Math.random();
  let res;
  if (r < 0.4) res = http.get(`${TARGET}/services?limit=20`, hdr(0, false));                       // cached list
  else if (r < 0.6) res = http.get(`${TARGET}/services/${ctx.event_ids[Math.floor(Math.random() * 50)]}`, hdr(0, false)); // detail
  else if (r < 0.8) res = http.get(`${TARGET}/services/${ctx.drop}/seats`, hdr(0, false));         // seat map
  else res = http.get(`${TARGET}/services?limit=20&offset=${Math.floor(Math.random() * 280)}&min_price=${Math.floor(Math.random() * 40)}`, hdr(0, false)); // cache miss -> DB
  track(res);
  check(res, { 'browse 200': (x) => x.status === 200 });
}

export function drop() {
  const i = __VU; // one user per VU
  const seat = ctx.seat_ids[Math.floor(Math.random() * ctx.seat_ids.length)];
  const res = http.post(`${TARGET}/bookings`,
    JSON.stringify({ service_id: ctx.drop, quantity: 1, assigned_unit_ids: [seat] }), hdr(i, true));
  track(res);
  if (res.status === 201) seatsWon.add(1); else if (res.status === 409) seatsLost.add(1);
  // 201 = got a seat, 409 = seat taken (expected). Anything else is a problem.
  check(res, { 'drop 201 or 409': (x) => x.status === 201 || x.status === 409 });
}

export function book() {
  const res = http.post(`${TARGET}/bookings`,
    JSON.stringify({ service_id: ctx.big, quantity: 1 }), hdr(Math.floor(Math.random() * tokens.length), true));
  track(res);
  // 400 = a user hit the max-5-pending-holds rule; fine. 201 = hold created.
  check(res, { 'book 201 or 400': (x) => x.status === 201 || x.status === 400 });
}

export function handleSummary(data) {
  const m = data.metrics;
  const won = m.seats_won_201 ? m.seats_won_201.values.count : 0;
  const lost = m.seats_lost_409 ? m.seats_lost_409.values.count : 0;
  const c503 = m.pool_exhausted_503 ? m.pool_exhausted_503.values.count : 0;
  const c429 = m.rate_limited_429 ? m.rate_limited_429.values.count : 0;

  const dropSummary = [
    '',
    '================================================================================',
    'TICKET DROP SCENARIO REPORT:',
    `  Seats Won (201):        ${won} (must be <= 500 seats; overselling if > 500)`,
    `  Seats Lost (409):       ${lost} (conflicts / seat already taken)`,
    `  Pool Exhausted (503):   ${c503} (DB connection pool timeout)`,
    `  Rate Limited (429):     ${c429} (per-IP rate limit tripped)`,
    '================================================================================',
    ''
  ].join('\n');

  let defaultTxt = '';
  try {
    defaultTxt = textSummary(data, { indent: ' ', enableColors: true });
  } catch (e) {
    defaultTxt = 'Default summary formatting unavailable.';
  }

  return {
    'stdout': defaultTxt + '\n' + dropSummary,
    'summary.json': JSON.stringify(data, null, 2),
  };
}
