# CURA Implementation Plan

## Project Overview
- **Current system state**: The CURA system consists of a separate React frontend (Vite) and a Django REST Framework backend.
- **Existing frontend condition**: Fully developed UI featuring comprehensive dashboards, login functionality, and data visualization. All data interactions currently depend entirely on static mock arrays.
- **Backend status**: Initial Django setup completed with REST framework and CORS configured. A basic `/api/health/` endpoint is active and verified to connect with the React frontend.
- **Current mock data usage**: The frontend utilizes robust TypeScript interfaces populated by local mock data arrays in `src/features/dashboard/services/mockData.ts` to simulate all application state.
- **Production Architecture Target**:
  - **Frontend**: React + Vite hosted on Vercel.
  - **Backend**: Django REST Framework hosted on Render.
  - **Database**: Supabase PostgreSQL.

## Development Strategy
- **Gradual Development**: Build CURA systematically, part-by-part. Avoid unnecessary complexity outside the current scope.
- **Temporary Mock Data**: Do not remove `mockData.ts` immediately. Leave it intact so the UI remains fully testable while the backend is constructed.
- **Modular Replacement**: Replace static arrays with async API calls module by module once the respective Django endpoints are verified.
- **Testing Approach**: Test every backend module via Postman/DRF browsable API before attempting frontend integration.

---

# Phase 1: Backend Foundation & Supabase
- **Django project setup**: Ensure base configuration is stable, apps are structured (e.g., `api`, `users`, `core`).
- **Target Database (Supabase PostgreSQL)**: Design the system explicitly around PostgreSQL. Remove any previous assumptions about SQLite.
- **Environment Configuration**: Setup `.env` files using `python-decouple`, utilizing a `DATABASE_URL` connection string strictly connecting to the Supabase instance.
- **Audit Logging System**: Implement a robust audit logging mechanism right from the start to track critical user actions (who changed data, what exactly was changed, and the precise timestamp).

---

# Phase 2: Database Design & Roles
- **Models implementation order**: 
  1. Users (Custom User Model)
  2. Audit Logs
  3. Patient
  4. MedicineItem & StockHistory (Inventory)
  5. Consultation & Treatment
  6. MedicalCertificate
  7. Bed & BedHistory
  8. PurchaseRequest & PurchaseHistory
  9. HospitalTransfer
  10. AppNotification
- **Relationships**: Define strict One-to-Many and Many-to-Many relationships optimized for PostgreSQL indexing and foreign key constraints.
- **Roles & Authentication Structure**:
  - Admin (Superuser)
  - Nurse
  - Doctor
  - Staff
- **Important**: Nurse/Doctor/Staff accounts are strictly manually created by the Admin. There is absolutely no public registration system.

---

# Phase 3: Authentication System
- **JWT Authentication**: Implement Django REST Framework JWT authentication using `djangorestframework-simplejwt`.
- **Flow**: Separate frontend/backend authentication flows. The React frontend will receive JWT access/refresh tokens and store them securely to attach to subsequent API requests.
- **Role-based access**: Setup DRF custom permission classes (e.g., `IsNurseOrDoctor`). Ensure endpoints aggressively reject unauthorized role access.

---

# Phase 4: Core Medical Modules

Implement in the following order:

1. **Patients**: Core entity.
2. **Consultations**: Requires Patients and Users (Doctors).
3. **Vital Signs**: Embedded in Consultations.
4. **Treatments/Medicine Given**: Tied to Consultations and Inventory.
5. **Medicine Inventory**: Tracks stock, tied to Treatments.
6. **Beds**: Tracks occupancy, linked to Patients.
7. **Medical Certificates**: Requires Consultations/Patients.
8. **Hospital Transfers**: Requires Consultations.
9. **Notifications**: System-wide event tracking.

**For each module include:**
- **Backend tasks**: Write Django Model and run migrations directly to Supabase.
- **API tasks**: Create Serializer and ModelViewSet, hook up to `urls.py`.
- **Frontend integration task**: Build the API service, swap out mock data for React state mapped to real API returns.
- **Testing task**: Test endpoint and verify UI data rendering. Ensure the new Audit Logs accurately record the module's changes.

---

# Phase 5: API Development
- **Django REST Framework**: Follow the order outlined in Phase 4.
- **Serializers**: Focus heavily on nested serializers for seamless JSON delivery matching the exact mock data structure defined in the frontend TypeScript interfaces.
- **ViewSets & Routing**: Use `ModelViewSet` and DRF `DefaultRouter` for automatic, clean URL generation.
- **API testing**: Use `pytest-django` or `APITestCase` to validate response formats and HTTP status codes.

---

# Phase 6: Frontend Integration
- **Vercel Frontend Environment**: Setup API base URL configuration dynamically using environment variables (e.g., `VITE_API_URL`).
- **Secure Communication**: Ensure Axios/Fetch API services are configured to seamlessly intercept and attach JWT tokens to the `Authorization` header when communicating with the Render backend.
- **Replace mock data module-by-module**: Systematically replace static arrays with async API calls.
- **Error handling**: Implement global error catching (showing toast notifications on 400/500 errors).
- **Loading states**: Add skeleton loaders or spinners to the UI while fetching real data to maintain a polished UX.

---

# Phase 7: Realtime System
- **Development Environment**: Django Channels configured for local testing.
- **Production Environment**: Django Channels hosted on Render, utilizing Redis as the channel layer. The Vercel React frontend connects via secure WebSockets to receive realtime updates.
- **Realtime features**:
  - **Patient queue updates**: Live sync of waiting lists across all terminals.
  - **Consultation status updates**: Instant status shifts (e.g., waiting -> in-progress).
  - **Medicine stock alerts**: Instant low-stock notifications.
  - **Bed status updates**: Immediately reflect when a bed becomes Available/Occupied.
  - **Notifications**: Push system-wide alerts instantly.

---

# Phase 8: Testing
- **Backend testing**: Ensure all models have database constraints tested (e.g., preventing negative inventory stock via PostgreSQL checks).
- **API testing**: Verify all endpoints reject unauthorized access and correctly issue/refresh JWTs.
- **Frontend testing**: Verify component rendering with the new async data flow on a local network.
- **User role testing**: Login as Nurse, Doctor, Staff, and Admin separately to guarantee role-based access control works.
- **Audit testing**: Verify that database changes reliably trigger the correct audit log entries.

---

# Phase 9: Deployment Preparation
- **Backend Deployment Planning (Render)**:
  - Configure `gunicorn` as the WSGI HTTP Server.
  - Manage all sensitive environment variables securely within Render's dashboard.
  - Setup the `DATABASE_URL` string to point directly to Supabase.
  - Configure strict CORS settings in `settings.py` specifically allowing ONLY the Vercel frontend URL.
  - Lock down Production security settings (`DEBUG = False`, `SECURE_SSL_REDIRECT`, etc.).
- **Frontend Deployment Planning (Vercel)**:
  - Ensure Vercel build settings accurately execute `npm run build`.
  - Validate production environment variables (`VITE_API_URL` pointing to the Render domain).
- **Database Migration**: Execute final `python manage.py migrate` against the production Supabase instance.

---

# Phase 10: Future Expansion (Planning Only - Do Not Implement Yet)
- **Appointment scheduling**: Allow for future booking slots.
- **Doctor availability**: Calendar integrations for tracking when doctors are on duty.
- **Video consultation/WebRTC**: Telehealth features for remote patient diagnosis.
