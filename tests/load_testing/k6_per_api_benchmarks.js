import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

// ============================================================================
// Production-Level Per-API Load & Saturation Capacity Benchmark
// Measures maximum throughput (RPS), p95 latency, and bottleneck thresholds
// for every core API subsystem in isolation.
// ============================================================================

export const healthRps = new Counter('health_requests_total');
export const servicesRps = new Counter('services_requests_total');
export const seatsRps = new Counter('seats_requests_total');
export const profileRps = new Counter('profile_requests_total');
export const loginRps = new Counter('login_requests_total');
export const bookingRps = new Counter('booking_requests_total');

export const healthDuration = new Trend('health_duration_ms', true);
export const servicesDuration = new Trend('services_duration_ms', true);
export const seatsDuration = new Trend('seats_duration_ms', true);
export const profileDuration = new Trend('profile_duration_ms', true);
export const loginDuration = new Trend('login_duration_ms', true);
export const bookingDuration = new Trend('booking_duration_ms', true);

export const options = {
  scenarios: {
    // 1. Health Endpoint (Baseline ASGI & Nginx Ceiling)
    test_health: {
      executor: 'ramping-vus',
      startVUs: 10,
      stages: [
        { duration: '3s', target: 100 },
        { duration: '7s', target: 300 },
        { duration: '2s', target: 0 },
      ],
      startTime: '0s',
      gracefulStop: '2s',
      exec: 'testHealth',
    },

    // 2. Event Catalog (Redis Cached JSON + Gzip)
    test_services: {
      executor: 'ramping-vus',
      startVUs: 10,
      stages: [
        { duration: '3s', target: 200 },
        { duration: '7s', target: 500 },
        { duration: '2s', target: 0 },
      ],
      startTime: '13s',
      gracefulStop: '2s',
      exec: 'testServices',
    },

    // 3. Seat Map (Large Payload JSON Serialization)
    test_seats: {
      executor: 'ramping-vus',
      startVUs: 10,
      stages: [
        { duration: '3s', target: 100 },
        { duration: '7s', target: 300 },
        { duration: '2s', target: 0 },
      ],
      startTime: '26s',
      gracefulStop: '2s',
      exec: 'testSeats',
    },

    // 4. Authenticated User Profile (JWT Decode + DB Single Row Read)
    test_profile: {
      executor: 'ramping-vus',
      startVUs: 10,
      stages: [
        { duration: '3s', target: 100 },
        { duration: '7s', target: 250 },
        { duration: '2s', target: 0 },
      ],
      startTime: '39s',
      gracefulStop: '2s',
      exec: 'testProfile',
    },

    // 5. Authentication & Login (CPU-Bound Argon2id Password Hashing)
    test_login: {
      executor: 'ramping-vus',
      startVUs: 5,
      stages: [
        { duration: '3s', target: 20 },
        { duration: '7s', target: 50 },
        { duration: '2s', target: 0 },
      ],
      startTime: '52s',
      gracefulStop: '2s',
      exec: 'testLogin',
    },

    // 6. Transactional Seat Hold (PostgreSQL SELECT FOR UPDATE ACID Lock)
    test_booking: {
      executor: 'ramping-vus',
      startVUs: 5,
      stages: [
        { duration: '3s', target: 30 },
        { duration: '7s', target: 80 },
        { duration: '2s', target: 0 },
      ],
      startTime: '65s',
      gracefulStop: '2s',
      exec: 'testBooking',
    },
  },
  thresholds: {
    'http_req_failed': ['rate<0.05'], // Allow <5% error under peak saturation
  },
};

const BASE_URL = __ENV.TARGET_URL || 'http://localhost';

// Setup phase: Obtain pre-authenticated buyer and seller tokens for test execution
export function setup() {
  const loginRes = http.post(`${BASE_URL}/login`, 'username=seller@loadtest.com&password=Password123!', {
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  });

  let sellerToken = '';
  if (loginRes.status === 200 && loginRes.json('access_token')) {
    sellerToken = loginRes.json('access_token');
  }

  const buyerRes = http.post(`${BASE_URL}/login`, 'username=buyer1@loadtest.com&password=Password123!', {
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  });

  let buyerToken = '';
  if (buyerRes.status === 200 && buyerRes.json('access_token')) {
    buyerToken = buyerRes.json('access_token');
  }

  return { sellerToken, buyerToken };
}

// Helper: unique IP per VU to validate rate limiter forwarding
function getVUIp(vu) {
  const subnet = Math.floor(vu / 250);
  const host = (vu % 250) + 1;
  return `198.51.${100 + subnet}.${host}`;
}

// ----------------------------------------------------------------------------
// 1. Health Check
// ----------------------------------------------------------------------------
export function testHealth() {
  const res = http.get(`${BASE_URL}/`, {
    headers: { 'X-Forwarded-For': getVUIp(__VU), 'Accept': 'application/json' },
  });
  healthRps.add(1);
  healthDuration.add(res.timings.duration);
  check(res, { 'health is 200': (r) => r.status === 200 });
  sleep(0.05);
}

// ----------------------------------------------------------------------------
// 2. Services Catalog (Redis Read-Through Cache)
// ----------------------------------------------------------------------------
export function testServices() {
  const res = http.get(`${BASE_URL}/services?limit=20`, {
    headers: { 'X-Forwarded-For': getVUIp(__VU), 'Accept': 'application/json' },
  });
  servicesRps.add(1);
  servicesDuration.add(res.timings.duration);
  check(res, { 'services is 200': (r) => r.status === 200 });
  sleep(0.05);
}

// ----------------------------------------------------------------------------
// 3. Seat Map (Large Payload)
// ----------------------------------------------------------------------------
export function testSeats() {
  const res = http.get(`${BASE_URL}/services/1/seats`, {
    headers: { 'X-Forwarded-For': getVUIp(__VU), 'Accept': 'application/json' },
  });
  seatsRps.add(1);
  seatsDuration.add(res.timings.duration);
  check(res, { 'seats is 200': (r) => r.status === 200 });
  sleep(0.05);
}

// ----------------------------------------------------------------------------
// 4. Authenticated User Profile (JWT Decode + DB Read)
// ----------------------------------------------------------------------------
export function testProfile(data) {
  const headers = {
    'X-Forwarded-For': getVUIp(__VU),
    'Accept': 'application/json',
    'Authorization': `Bearer ${data.sellerToken}`,
  };
  const res = http.get(`${BASE_URL}/account/me`, { headers });
  profileRps.add(1);
  profileDuration.add(res.timings.duration);
  check(res, { 'profile is 200': (r) => r.status === 200 });
  sleep(0.05);
}

// ----------------------------------------------------------------------------
// 5. Authentication & Login (Argon2id CPU Bound)
// ----------------------------------------------------------------------------
export function testLogin() {
  const payload = 'username=seller@loadtest.com&password=Password123!';
  const headers = {
    'Content-Type': 'application/x-www-form-urlencoded',
    'X-Forwarded-For': getVUIp(__VU),
  };
  const res = http.post(`${BASE_URL}/login`, payload, { headers });
  loginRps.add(1);
  loginDuration.add(res.timings.duration);
  check(res, { 'login handled (200 or 429)': (r) => r.status === 200 || r.status === 429 });
  sleep(0.1);
}

// ----------------------------------------------------------------------------
// 6. Transactional Booking Hold (Postgres SELECT FOR UPDATE)
// ----------------------------------------------------------------------------
export function testBooking(data) {
  const payload = JSON.stringify({
    service_id: 2,
    quantity: 1,
    tier_id: 3,
  });
  const headers = {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
    'Authorization': `Bearer ${data.buyerToken}`,
    'X-Forwarded-For': getVUIp(__VU),
  };
  const res = http.post(`${BASE_URL}/bookings`, payload, { headers });
  bookingRps.add(1);
  bookingDuration.add(res.timings.duration);
  check(res, { 'booking handled (201, 400 hold limit, or 409 conflict)': (r) => r.status === 201 || r.status === 400 || r.status === 409 });
  sleep(0.1);
}
