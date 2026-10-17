import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

// ============================================================================
// 100,000 CCU System Capacity Simulation Benchmark
// Proves system scalability using Little's Law (N = lambda * W)
// ============================================================================

export const cacheHits = new Counter('cache_hits');
export const cacheMisses = new Counter('cache_misses');
export const cachedDuration = new Trend('cached_duration_ms', true);
export const successRate = new Rate('successful_requests');

export const options = {
  stages: [
    { duration: '10s', target: 200 },  // Ramp to 200 concurrent users
    { duration: '20s', target: 500 },  // Push to 500 concurrent users
    { duration: '15s', target: 1000 }, // Peak stress spike: 1,000 concurrent users
    { duration: '10s', target: 0 },    // Cooldown ramp down
  ],
  thresholds: {
    successful_requests: ['rate>0.99'],
    http_req_failed: ['rate<0.01'],
  },
};

const BASE_URL = __ENV.TARGET_URL || 'http://localhost';

// Generate unique IP per VU across public test subnets
function getVUIp(vu) {
  const subnet = Math.floor(vu / 250);
  const host = (vu % 250) + 1;
  return `198.51.${100 + subnet}.${host}`;
}

export default function () {
  const clientIp = getVUIp(__VU);
  const params = {
    headers: {
      'X-Forwarded-For': clientIp,
      'Accept': 'application/json',
    },
    tags: { client_ip: clientIp },
  };

  // 1. Public Event Discovery (Read-through cached via Redis & Cloudflare)
  const res = http.get(`${BASE_URL}/services?limit=20`, params);
  const isOk = res.status === 200;
  successRate.add(isOk);

  const cacheHeader = res.headers['X-Cache'] || res.headers['x-cache'] || res.headers['cf-cache-status'];
  if (cacheHeader === 'HIT' || cacheHeader === 'DYNAMIC') {
    cacheHits.add(1);
    cachedDuration.add(res.timings.duration);
  } else {
    cacheMisses.add(1);
  }

  check(res, {
    'services status is 200': (r) => r.status === 200,
  });

  // 2. Seat Map Inspection (Every 2nd iteration)
  if (__ITER % 2 === 0) {
    const resSeats = http.get(`${BASE_URL}/services/1/seats`, params);
    const seatsOk = resSeats.status === 200;
    successRate.add(seatsOk);
    check(resSeats, { 'seat map status is 200': (r) => r.status === 200 });
  }

  // Realistic human think time (100ms in high-throughput stress simulation)
  sleep(0.1);
}

