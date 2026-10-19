import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

// Custom metrics for clean reporting
const ticketsWon = new Counter('tickets_won');
const soldOut = new Counter('sold_out');
const rateLimited = new Counter('rate_limited');
const serverErrors = new Counter('server_errors');

// Load seeded context
const context = JSON.parse(open('./drop_test_context.json'));
const serviceId = context.service_id;
const maxCapacity = context.max_capacity;
const buyers = context.buyers;

export const options = {
  scenarios: {
    drop_race: {
      executor: 'per-vu-iterations',
      vus: buyers.length,
      iterations: 1,
      maxDuration: '30s',
    },
  },
  thresholds: {
    'server_errors': ['count==0'],
  },
};

export default function () {
  const vuIndex = (__VU - 1) % buyers.length;
  const buyer = buyers[vuIndex];
  const target = __ENV.TARGET || 'http://localhost';

  const url = `${target}/bookings`;
  const payload = JSON.stringify({
    service_id: serviceId,
    quantity: 1,
  });

  const headers = {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${buyer.token}`,
    'X-Forwarded-For': `198.51.100.${__VU}`,
  };

  const res = http.post(url, payload, { headers, timeout: '15s' });

  if (res.status === 201) {
    ticketsWon.add(1);
  } else if (res.status === 409) {
    soldOut.add(1);
  } else if (res.status === 429) {
    rateLimited.add(1);
  } else if (res.status >= 500) {
    serverErrors.add(1);
  }

  check(res, {
    'valid response status (201 or 409)': (r) => r.status === 201 || r.status === 409,
    'no 5xx server error': (r) => r.status < 500,
  });
}
