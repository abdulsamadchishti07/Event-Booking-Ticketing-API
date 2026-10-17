import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

// ============================================================================
// Custom Metrics for Sprint 6 Nginx & Load Testing
// ============================================================================
export const cacheHits = new Counter('cache_hits');
export const cacheMisses = new Counter('cache_misses');
export const cachedDuration = new Trend('cached_req_duration', true);
export const successRate = new Rate('successful_requests');

// ============================================================================
// k6 Load Test Configuration: User Capacity & Concurrency Ramp-Up
// ============================================================================
export const options = {
  stages: [
    { duration: '10s', target: 50 },   // Stage 1: 50 concurrent users from 50 IPs (baseline)
    { duration: '15s', target: 150 },  // Stage 2: 150 concurrent users from 150 IPs (medium load)
    { duration: '20s', target: 300 },  // Stage 3: 300 concurrent users from 300 IPs (high capacity)
    { duration: '15s', target: 500 },  // Stage 4: 500 concurrent users from 500 IPs (peak stress)
    { duration: '10s', target: 0 },    // Stage 5: Ramp-down & recovery
  ],
  thresholds: {
    // 95% of all HTTP requests should complete in under 50ms
    http_req_duration: ['p(95)<50', 'p(99)<100'],
    // Error rate must remain under 1%
    http_req_failed: ['rate<0.01'],
    // Successful requests rate must exceed 99%
    successful_requests: ['rate>0.99'],
  },
};

// Target URL defaults to Nginx gateway on port 80
const BASE_URL = __ENV.TARGET_URL || 'http://localhost';

// Generate a deterministic, realistic unique client IP per Virtual User (VU)
// Uses standard non-routable public test subnets: 198.51.100.0/24, 203.0.113.0/24, 192.0.2.0/24
function getClientIp(vu) {
  const subnet = Math.floor(vu / 250);
  const host = (vu % 250) + 1;
  if (subnet === 0) return `198.51.100.${host}`;
  if (subnet === 1) return `203.0.113.${host}`;
  if (subnet === 2) return `192.0.2.${host}`;
  return `198.51.${101 + subnet}.${host}`;
}

export default function () {
  const clientIp = getClientIp(__VU);
  const params = {
    headers: {
      'X-Forwarded-For': clientIp,
      'Accept': 'application/json',
    },
    tags: { client_ip: clientIp },
  };

  // 1. Primary: Event Discovery (Exercises Redis cache read-through & Nginx gzip)
  const resDiscovery = http.get(`${BASE_URL}/services?limit=20`, params);
  const is200 = resDiscovery.status === 200;
  successRate.add(is200);

  const cacheHeader = resDiscovery.headers['X-Cache'] || resDiscovery.headers['x-cache'];
  if (cacheHeader === 'HIT') {
    cacheHits.add(1);
    cachedDuration.add(resDiscovery.timings.duration);
  } else if (cacheHeader === 'MISS') {
    cacheMisses.add(1);
  }

  check(resDiscovery, {
    'discovery status 200': (r) => r.status === 200,
    'cache header present': (r) => (r.headers['X-Cache'] || r.headers['x-cache']) !== undefined,
    'sub-50ms latency': (r) => r.timings.duration < 50,
  });

  // 2. Secondary: Venue / Location Listing
  if (__ITER % 2 === 0) {
    const resLoc = http.get(`${BASE_URL}/locations`, params);
    check(resLoc, {
      'locations status 200': (r) => r.status === 200,
    });
  }

  // 3. Tertiary: Live Visual Seat Map Inventory (Unit-assigned event 1)
  if (__ITER % 3 === 0) {
    const resSeats = http.get(`${BASE_URL}/services/1/seats`, params);
    check(resSeats, {
      'seats status 200': (r) => r.status === 200,
      'seats sub-50ms': (r) => r.timings.duration < 50,
    });
  }

  // Realistic human think time between requests (100ms - 250ms)
  sleep(0.1 + Math.random() * 0.15);
}
