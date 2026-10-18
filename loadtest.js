// k6 load test for the Event Booking API.
//   k6 run -e TARGET=http://<server-ip> -e BROWSE_RPS=500 -e BOOK_RPS=50 loadtest.js
// Raise BROWSE_RPS / BOOK_RPS step by step; the last step that passes the thresholds is your capacity.
// Scenarios: "browse" (cached reads + cache misses) and "drop" (many users fight for 500 seats at once).
import http from 'k6/http';
import { check } from 'k6';
import { SharedArray } from 'k6/data';
import { Counter } from 'k6/metrics';

const ctx = JSON.parse(open('./loadctx.json'));
const tokens = new SharedArray('tokens', () => ctx.tokens);
const TARGET = __ENV.TARGET || 'http://localhost';
const BROWSE_RPS = parseInt(__ENV.BROWSE_RPS || '200');
const BOOK_RPS = parseInt(__ENV.BOOK_RPS || '20');
const HOLD = __ENV.HOLD || '3m';            // time at full rate
const DROP_USERS = parseInt(__ENV.DROP_USERS || '2000');

const serverErrors = new Counter('server_errors_5xx');
const seatsWon = new Counter('seats_won_201');
const seatsLost = new Counter('seats_lost_409');

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

// Pretend each request comes from a different client. This only works if the test traffic goes
// through your nginx (which overwrites X-Forwarded-For). Without it, all users share one IP and
// your rate limiter will (correctly) block them.
function hdr(i, auth) {
  const h = { 'Content-Type': 'application/json' };
  if (auth) h.Authorization = `Bearer ${tokens[i % tokens.length]}`;
  return { headers: h };
}
function track(res) { if (res.status >= 500) serverErrors.add(1); }

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
  console.log(`\nDROP RESULT: seats won=${won} (must be <= 500). If > 500 you have an OVERSELLING BUG.`);
  return { 'summary.json': JSON.stringify(data, null, 2) };
}
