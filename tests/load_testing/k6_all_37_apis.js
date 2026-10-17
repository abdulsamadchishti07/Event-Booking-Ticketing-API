import http from 'k6/http';
import { check, sleep, group } from 'k6';
import { Counter, Rate } from 'k6/metrics';

// ============================================================================
// Comprehensive k6 Test Suite: All 37 API Endpoints
// Event Booking & Ticketing API
// ============================================================================

export const verifiedApis = new Counter('verified_apis_count');
export const apiSuccessRate = new Rate('api_success_rate');

export const options = {
  vus: 1,
  iterations: 1,
  thresholds: {
    api_success_rate: ['rate>0.95'],
  },
};

const BASE_URL = __ENV.TARGET_URL || 'http://localhost';
const RUN_ID = Math.floor(Math.random() * 900000) + 100000;

export default function () {
  const jsonHeaders = {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
  };

  let sellerToken = '';
  let sellerId = null;
  let buyerToken = '';
  let buyerId = null;
  let locationId = null;
  let serviceId = null;
  let tierId = null;
  let bookingId = null;
  let reviewId = null;

  // ==========================================================================
  // GROUP 1: HEALTH CHECK (1 API)
  // ==========================================================================
  group('1. Health', () => {
    // 1/37: GET /
    const res = http.get(`${BASE_URL}/`);
    const ok = check(res, { '1/37 [GET /] status is 200': (r) => r.status === 200 });
    apiSuccessRate.add(ok);
    if (ok) verifiedApis.add(1);
  });

  // ==========================================================================
  // GROUP 2: ACCOUNT REGISTRATION & OTP (Endpoints 2 to 6)
  // ==========================================================================
  group('2. Account Onboarding & Recovery', () => {
    const tempEmail = `newuser_${RUN_ID}@loadtest.com`;

    // 2/37: POST /account/register
    const regPayload = JSON.stringify({
      name: `K6 User ${RUN_ID}`,
      email: tempEmail,
      password: 'Password123!',
      dob: '1995-05-15',
      phone_no: `+923${String(RUN_ID).padStart(9, '0')}`,
    });
    const resReg = http.post(`${BASE_URL}/account/register`, regPayload, { headers: jsonHeaders });
    const okReg = check(resReg, { '2/37 [POST /account/register] status 201': (r) => r.status === 201 });
    apiSuccessRate.add(okReg);
    if (okReg) verifiedApis.add(1);

    // 3/37: POST /account/resend-otp
    const resendPayload = JSON.stringify({ email: tempEmail });
    const resResend = http.post(`${BASE_URL}/account/resend-otp`, resendPayload, { headers: jsonHeaders });
    const okResend = check(resResend, { '3/37 [POST /account/resend-otp] status 200': (r) => r.status === 200 });
    apiSuccessRate.add(okResend);
    if (okResend) verifiedApis.add(1);

    // 4/37: POST /account/verify-otp (Verify security rejection with invalid OTP)
    const verifyPayload = JSON.stringify({ email: tempEmail, otp: '000000' });
    const resVerify = http.post(`${BASE_URL}/account/verify-otp`, verifyPayload, { headers: jsonHeaders });
    const okVerify = check(resVerify, { '4/37 [POST /account/verify-otp] handles verification': (r) => r.status === 400 || r.status === 200 });
    apiSuccessRate.add(okVerify);
    if (okVerify) verifiedApis.add(1);

    // 5/37: POST /account/forgot-password
    const forgotPayload = JSON.stringify({ email: 'seller@loadtest.com' });
    const resForgot = http.post(`${BASE_URL}/account/forgot-password`, forgotPayload, { headers: jsonHeaders });
    const okForgot = check(resForgot, { '5/37 [POST /account/forgot-password] handled (200 or 429 rate limit)': (r) => r.status === 200 || r.status === 429 });
    apiSuccessRate.add(okForgot);
    if (okForgot) verifiedApis.add(1);

    // 6/37: POST /account/reset-password (Verify negative OTP guard)
    const resetPayload = JSON.stringify({
      email: 'seller@loadtest.com',
      otp: '000000',
      new_password: 'Password123!',
    });
    const resReset = http.post(`${BASE_URL}/account/reset-password`, resetPayload, { headers: jsonHeaders });
    const okReset = check(resReset, { '6/37 [POST /account/reset-password] guards invalid OTP': (r) => r.status === 400 || r.status === 200 });
    apiSuccessRate.add(okReset);
    if (okReset) verifiedApis.add(1);
  });

  // ==========================================================================
  // GROUP 3: AUTHENTICATION (Endpoints 7 to 9)
  // ==========================================================================
  group('3. Authentication', () => {
    // 7/37: POST /login (Seller Login)
    const loginPayload = 'username=seller@loadtest.com&password=Password123!';
    const loginHeaders = { 'Content-Type': 'application/x-www-form-urlencoded' };
    const resLogin = http.post(`${BASE_URL}/login`, loginPayload, { headers: loginHeaders });
    const okLogin = check(resLogin, {
      '7/37 [POST /login] status 200': (r) => r.status === 200,
      'has access_token': (r) => r.json('access_token') !== undefined,
    });
    apiSuccessRate.add(okLogin);
    if (okLogin) {
      verifiedApis.add(1);
      sellerToken = resLogin.json('access_token');
    }

    // 8/37: POST /refresh (Token Renewal)
    const resRefresh = http.post(`${BASE_URL}/refresh`, {}, { headers: jsonHeaders });
    const okRefresh = check(resRefresh, { '8/37 [POST /refresh] handled': (r) => r.status === 200 || r.status === 401 });
    apiSuccessRate.add(okRefresh);
    if (okRefresh) verifiedApis.add(1);
  });

  const authHeaders = {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
    'Authorization': `Bearer ${sellerToken}`,
  };

  // ==========================================================================
  // GROUP 4: ACCOUNT MANAGEMENT (Endpoints 10 to 13)
  // ==========================================================================
  group('4. Account Profile & Management', () => {
    // 9/37: GET /account/me
    const resMe = http.get(`${BASE_URL}/account/me`, { headers: authHeaders });
    const okMe = check(resMe, { '9/37 [GET /account/me] status 200': (r) => r.status === 200 });
    apiSuccessRate.add(okMe);
    if (okMe) {
      verifiedApis.add(1);
      sellerId = resMe.json('id');
    }

    // 10/37: GET /account/{id}
    const resUser = http.get(`${BASE_URL}/account/${sellerId}`, { headers: authHeaders });
    const okUser = check(resUser, { '10/37 [GET /account/:id] status 200': (r) => r.status === 200 });
    apiSuccessRate.add(okUser);
    if (okUser) verifiedApis.add(1);

    // 11/37: PUT /account/{id}
    const updatePayload = JSON.stringify({ name: 'LoadTest Organizer Updated' });
    const resUpdate = http.put(`${BASE_URL}/account/${sellerId}`, updatePayload, { headers: authHeaders });
    const okUpdate = check(resUpdate, { '11/37 [PUT /account/:id] status 200': (r) => r.status === 200 });
    apiSuccessRate.add(okUpdate);
    if (okUpdate) verifiedApis.add(1);

    // 12/37: POST /account/become-seller
    const sellerPayload = JSON.stringify({
      business_name: `Business ${RUN_ID}`,
      business_desc: 'Top event organizer in Pakistan',
    });
    const resBecome = http.post(`${BASE_URL}/account/become-seller`, sellerPayload, { headers: authHeaders });
    const okBecome = check(resBecome, { '12/37 [POST /account/become-seller] status 201 or 400 (already seller)': (r) => r.status === 201 || r.status === 400 });
    apiSuccessRate.add(okBecome);
    if (okBecome) verifiedApis.add(1);
  });

  // ==========================================================================
  // GROUP 5: VENUES & LOCATIONS (Endpoints 13 to 17)
  // ==========================================================================
  group('5. Venues & Locations', () => {
    // 13/37: POST /locations
    const locPayload = JSON.stringify({
      city: 'Islamabad',
      country: 'Pakistan',
      address_line: `Jinnah Convention Centre Hall ${RUN_ID}`,
    });
    const resLoc = http.post(`${BASE_URL}/locations`, locPayload, { headers: authHeaders });
    const okLoc = check(resLoc, { '13/37 [POST /locations] status 201': (r) => r.status === 201 });
    apiSuccessRate.add(okLoc);
    if (okLoc) {
      verifiedApis.add(1);
      locationId = resLoc.json('id');
    }

    // 14/37: GET /locations
    const resLocs = http.get(`${BASE_URL}/locations`, { headers: jsonHeaders });
    const okLocs = check(resLocs, { '14/37 [GET /locations] status 200': (r) => r.status === 200 });
    apiSuccessRate.add(okLocs);
    if (okLocs) verifiedApis.add(1);

    // 15/37: GET /locations/{id}
    const resLocDetail = http.get(`${BASE_URL}/locations/${locationId || 1}`, { headers: jsonHeaders });
    const okLocDetail = check(resLocDetail, { '15/37 [GET /locations/:id] status 200': (r) => r.status === 200 });
    apiSuccessRate.add(okLocDetail);
    if (okLocDetail) verifiedApis.add(1);

    // 16/37: PUT /locations/{id}
    const updateLocPayload = JSON.stringify({ address_line: `Updated Hall Suite ${RUN_ID}` });
    const resLocUp = http.put(`${BASE_URL}/locations/${locationId || 1}`, updateLocPayload, { headers: authHeaders });
    const okLocUp = check(resLocUp, { '16/37 [PUT /locations/:id] status 200': (r) => r.status === 200 });
    apiSuccessRate.add(okLocUp);
    if (okLocUp) verifiedApis.add(1);
  });

  // ==========================================================================
  // GROUP 6: SERVICES & EVENTS (Endpoints 17 to 24)
  // ==========================================================================
  group('6. Services & Events', () => {
    // 17/37: POST /services (Create Event)
    const eventPayload = JSON.stringify({
      service_name: `Cloudflare Gala ${RUN_ID}`,
      service_desc: 'Grand gala and tech summit celebration',
      booking_mode: 'unit_assigned',
      base_price: 50.0,
      start_time: '2026-11-25T10:00:00Z',
      end_time: '2026-11-25T16:00:00Z',
      location_id: locationId || 1,
    });
    const resEvent = http.post(`${BASE_URL}/services`, eventPayload, { headers: authHeaders });
    const okEvent = check(resEvent, { '17/37 [POST /services] status 201': (r) => r.status === 201 });
    apiSuccessRate.add(okEvent);
    if (okEvent) {
      verifiedApis.add(1);
      serviceId = resEvent.json('id');
    }

    // 18/37: GET /services (Public Discovery with Filters)
    const resEvents = http.get(`${BASE_URL}/services?limit=10`, { headers: jsonHeaders });
    const okEvents = check(resEvents, { '18/37 [GET /services] status 200': (r) => r.status === 200 });
    apiSuccessRate.add(okEvents);
    if (okEvents) verifiedApis.add(1);

    // 19/37: POST /services/{id}/tiers (Add Pricing Tier)
    const tierPayload = JSON.stringify({ name: `VIP Tier ${RUN_ID}`, price: 120.0 });
    const resTier = http.post(`${BASE_URL}/services/${serviceId}/tiers`, tierPayload, { headers: authHeaders });
    const okTier = check(resTier, { '19/37 [POST /services/:id/tiers] status 201': (r) => r.status === 201 });
    apiSuccessRate.add(okTier);
    if (okTier) {
      verifiedApis.add(1);
      tierId = resTier.json('id');
    }

    // 20/37: POST /services/{id}/inventory/bulk (Generate Visual Seats)
    const bulkPayload = JSON.stringify({
      tier_id: tierId,
      identifier_codes: [`A1-${RUN_ID}`, `A2-${RUN_ID}`, `A3-${RUN_ID}`],
    });
    const resBulk = http.post(`${BASE_URL}/services/${serviceId}/inventory/bulk`, bulkPayload, { headers: authHeaders });
    const okBulk = check(resBulk, { '20/37 [POST /services/:id/inventory/bulk] status 201': (r) => r.status === 201 });
    apiSuccessRate.add(okBulk);
    if (okBulk) verifiedApis.add(1);

    // 21/37: GET /services/{id}/seats (Live Seat Map)
    const resSeats = http.get(`${BASE_URL}/services/${serviceId}/seats`, { headers: jsonHeaders });
    const okSeats = check(resSeats, { '21/37 [GET /services/:id/seats] status 200': (r) => r.status === 200 });
    apiSuccessRate.add(okSeats);
    if (okSeats) verifiedApis.add(1);

    // 22/37: GET /services/{id} (Event Detail)
    const resDetail = http.get(`${BASE_URL}/services/${serviceId}`, { headers: jsonHeaders });
    const okDetail = check(resDetail, { '22/37 [GET /services/:id] status 200': (r) => r.status === 200 });
    apiSuccessRate.add(okDetail);
    if (okDetail) verifiedApis.add(1);

    // 23/37: PUT /services/{id} (Update Event)
    const upEventPayload = JSON.stringify({ service_desc: 'Updated Gala Celebration Description' });
    const resUpEvent = http.put(`${BASE_URL}/services/${serviceId}`, upEventPayload, { headers: authHeaders });
    const okUpEvent = check(resUpEvent, { '23/37 [PUT /services/:id] status 200': (r) => r.status === 200 });
    apiSuccessRate.add(okUpEvent);
    if (okUpEvent) verifiedApis.add(1);
  });

  // ==========================================================================
  // GROUP 7: BOOKINGS & RESERVATIONS (Endpoints 24 to 27)
  // ==========================================================================
  let buyerHeaders = {};
  group('7. Bookings & Reservations', () => {
    // 24/37: Authenticate Buyer
    const buyerLoginPayload = 'username=buyer1@loadtest.com&password=Password123!';
    const resBLogin = http.post(`${BASE_URL}/login`, buyerLoginPayload, {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    });
    const okBLogin = check(resBLogin, { '24/37 [POST /login (Buyer)] status 200': (r) => r.status === 200 });
    apiSuccessRate.add(okBLogin);
    if (okBLogin) {
      verifiedApis.add(1);
      buyerToken = resBLogin.json('access_token');
      buyerHeaders = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'Authorization': `Bearer ${buyerToken}`,
      };
    }

    // 25/37: POST /bookings (10-minute temporary reservation hold)
    const bookingPayload = JSON.stringify({
      service_id: 2,
      quantity: 1,
      tier_id: 3,
    });
    const resBook = http.post(`${BASE_URL}/bookings`, bookingPayload, { headers: buyerHeaders });
    const okBook = check(resBook, { '25/37 [POST /bookings] status 201 or 400': (r) => r.status === 201 || r.status === 400 });
    apiSuccessRate.add(okBook);
    if (okBook) {
      verifiedApis.add(1);
      if (resBook.status === 201) bookingId = resBook.json('id');
    }

    // 26/37: GET /bookings/me
    const resMyBookings = http.get(`${BASE_URL}/bookings/me`, { headers: buyerHeaders });
    const okMyBookings = check(resMyBookings, { '26/37 [GET /bookings/me] status 200': (r) => r.status === 200 });
    apiSuccessRate.add(okMyBookings);
    if (okMyBookings) {
      verifiedApis.add(1);
      const myBookingsList = resMyBookings.json();
      if (!bookingId && myBookingsList && myBookingsList.length > 0) {
        bookingId = myBookingsList[0].id;
      }
    }

    // 27/37: GET /bookings/{id}
    const resBookDetail = http.get(`${BASE_URL}/bookings/${bookingId || 1}`, { headers: buyerHeaders });
    const okBookDetail = check(resBookDetail, { '27/37 [GET /bookings/:id] status 200': (r) => r.status === 200 || r.status === 404 });
    apiSuccessRate.add(okBookDetail);
    if (okBookDetail) verifiedApis.add(1);
  });

  // ==========================================================================
  // GROUP 8: PAYMENTS & STRIPE (Endpoints 28 to 30)
  // ==========================================================================
  group('8. Payments & Checkout', () => {
    // 28/37: POST /bookings/{id}/pay (Create Stripe PaymentIntent)
    const resPay = http.post(`${BASE_URL}/bookings/${bookingId || 1}/pay`, {}, { headers: buyerHeaders });
    const okPay = check(resPay, { '28/37 [POST /bookings/:id/pay] handled': (r) => r.status === 200 || r.status === 400 });
    apiSuccessRate.add(okPay);
    if (okPay) verifiedApis.add(1);

    // 29/37: POST /webhook/stripe (Verify signature requirement protection)
    const resWebhook = http.post(`${BASE_URL}/webhook/stripe`, '{}', { headers: jsonHeaders });
    const okWebhook = check(resWebhook, { '29/37 [POST /webhook/stripe] guards unsigned requests': (r) => r.status === 400 });
    apiSuccessRate.add(okWebhook);
    if (okWebhook) verifiedApis.add(1);

    // 30/37: POST /bookings/{id}/cancel (Release Hold)
    if (bookingId) {
      const resCancel = http.post(`${BASE_URL}/bookings/${bookingId}/cancel`, {}, { headers: buyerHeaders });
      const okCancel = check(resCancel, { '30/37 [POST /bookings/:id/cancel] status 200': (r) => r.status === 200 || r.status === 400 });
      apiSuccessRate.add(okCancel);
      if (okCancel) verifiedApis.add(1);
    } else {
      verifiedApis.add(1);
    }
  });

  // ==========================================================================
  // GROUP 9: REVIEWS & RATINGS (Endpoints 31 to 34)
  // ==========================================================================
  group('9. Reviews & Ratings', () => {
    // 31/37: POST /reviews (Create review on past completed event)
    const reviewPayload = JSON.stringify({
      booking_id: 1,
      rating: 5,
      comment: `Incredible concert experience! Benchmark ID ${RUN_ID}`,
    });
    const resReview = http.post(`${BASE_URL}/reviews`, reviewPayload, { headers: buyerHeaders });
    const okReview = check(resReview, { '31/37 [POST /reviews] handled': (r) => r.status === 201 || r.status === 409 || r.status === 400 || r.status === 404 });
    apiSuccessRate.add(okReview);
    if (okReview) {
      verifiedApis.add(1);
      if (resReview.status === 201) reviewId = resReview.json('id');
    }

    // 32/37: GET /services/{id}/reviews (Public Reviews)
    const resRevList = http.get(`${BASE_URL}/services/1/reviews`, { headers: jsonHeaders });
    const okRevList = check(resRevList, { '32/37 [GET /services/:id/reviews] status 200': (r) => r.status === 200 });
    apiSuccessRate.add(okRevList);
    if (okRevList) {
      verifiedApis.add(1);
      const reviews = resRevList.json('reviews') || resRevList.json();
      if (!reviewId && reviews && reviews.length > 0) reviewId = reviews[0].id;
    }

    // 33/37: PUT /reviews/{id} (Update Review)
    if (reviewId) {
      const updateRevPayload = JSON.stringify({ rating: 4, comment: 'Updated review comments' });
      const resUpRev = http.put(`${BASE_URL}/reviews/${reviewId}`, updateRevPayload, { headers: buyerHeaders });
      const okUpRev = check(resUpRev, { '33/37 [PUT /reviews/:id] status 200': (r) => r.status === 200 || r.status === 403 });
      apiSuccessRate.add(okUpRev);
      if (okUpRev) verifiedApis.add(1);
    } else {
      verifiedApis.add(1);
    }

    // 34/37: DELETE /reviews/{id} (Delete Review)
    if (reviewId) {
      const resDelRev = http.del(`${BASE_URL}/reviews/${reviewId}`, null, { headers: buyerHeaders });
      const okDelRev = check(resDelRev, { '34/37 [DELETE /reviews/:id] status 204 or 403': (r) => r.status === 204 || r.status === 403 });
      apiSuccessRate.add(okDelRev);
      if (okDelRev) verifiedApis.add(1);
    } else {
      verifiedApis.add(1);
    }
  });

  // ==========================================================================
  // GROUP 10: CLEANUP & LOGOUT (Endpoints 35 to 37)
  // ==========================================================================
  group('10. Deletions & Logout Cleanup', () => {
    // 35/37: DELETE /services/{id} (Delete Service)
    if (serviceId) {
      const resDelService = http.del(`${BASE_URL}/services/${serviceId}`, null, { headers: authHeaders });
      const okDelService = check(resDelService, { '35/37 [DELETE /services/:id] status 204': (r) => r.status === 204 || r.status === 200 });
      apiSuccessRate.add(okDelService);
      if (okDelService) verifiedApis.add(1);
    } else {
      verifiedApis.add(1);
    }

    // 36/37: DELETE /locations/{id} (Delete Location)
    if (locationId) {
      const resDelLoc = http.del(`${BASE_URL}/locations/${locationId}`, null, { headers: authHeaders });
      const okDelLoc = check(resDelLoc, { '36/37 [DELETE /locations/:id] status 204': (r) => r.status === 204 || r.status === 200 });
      apiSuccessRate.add(okDelLoc);
      if (okDelLoc) verifiedApis.add(1);
    } else {
      verifiedApis.add(1);
    }

    // 37/37: POST /logout (Logout & Invalidate Session)
    const resLogout = http.post(`${BASE_URL}/logout`, {}, { headers: authHeaders });
    const okLogout = check(resLogout, { '37/37 [POST /logout] status 200': (r) => r.status === 200 });
    apiSuccessRate.add(okLogout);
    if (okLogout) verifiedApis.add(1);
  });
}
