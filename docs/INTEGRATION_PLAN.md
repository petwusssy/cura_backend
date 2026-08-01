# CURA Frontend-Backend Integration Plan

## 1. Current Architecture

Currently, the CURA project operates with two distinctly separated applications that are not yet fully integrated:

```text
React + Vite (Frontend)
        |
        |  (Currently disconnected; uses internal mock data arrays)
        |
Django REST API (Backend)
        |
        |  (Handles data storage and business logic)
        |
Database (Supabase PostgreSQL)
```

**Current Mock Data Flow:**
The frontend relies heavily on `src/features/dashboard/services/mockData.ts`. When a React component needs patient or consultation data, it imports these static TypeScript arrays directly instead of making HTTP requests.

**Target API Flow:**
React components will call specific functions from our frontend API Service Layer (e.g., `patientService.ts`), which will execute `Axios` requests over the network to the Django REST Framework endpoints. Django will fetch data from PostgreSQL, serialize it into JSON, and return it to the frontend.

---

# Phase 1: Local Development Environment & Foundation

Before integrating modules, ensure the local development environment is strictly defined:
- **Frontend Environment**: React + Vite running strictly on `http://localhost:5173`.
- **Backend Environment**: Django REST Framework running strictly on `http://localhost:8000`.
- **Database Environment**: Connected directly to the **Supabase PostgreSQL** instance.

**Foundation Tasks:**
- **Configure CORS properly**: Double-check `django-cors-headers` explicitly allows requests from `http://localhost:5173`.
- **Setup API base URL**: Define the backend endpoint structure.
- **Setup frontend environment variables**: Create a `.env.local` in the React frontend with `VITE_API_URL=http://localhost:8000/api`.
- **Create frontend API service layer**: Establish the foundational `axios` configuration with base URL and default headers. Do not connect any specific modules yet.

---

# Phase 2: Authentication Integration
- **Django authentication endpoint**: Set up DRF endpoints to accept credentials and issue JWTs using `djangorestframework-simplejwt`.
- **JWT authentication flow**: Implement token retrieval, storage, and refresh mechanisms. **The system must use secure, HTTP-only cookies for the refresh token** to prevent XSS attacks, while the short-lived access token can be held in memory or secure local storage.
- **Login request from React**: Connect the existing React login form to the Django login endpoint.
- **Attach JWT token to API requests**: Configure the Axios interceptor to append `Authorization: Bearer <token>` to all outgoing requests.
- **Handle logout**: Clear local tokens and invalidate the refresh cookie server-side.
- **Role-based user access**: Enforce frontend routing restrictions based on user roles embedded in the JWT payload.
- **Requirements**: 
  - No public registration forms.
  - Admin manually creates Nurse, Doctor, and Staff accounts.

---

# Phase 3: API Service Architecture
**Design frontend structure:**

To maintain a clean architecture, all external API calls will be abstracted into dedicated service files:

```text
frontend/src/
├── services/
│   ├── api.ts                  # Base Axios instance & Interceptors
│   ├── authService.ts          # Login, Logout, JWT handling
│   ├── patientService.ts       # Patient CRUD operations
│   ├── consultationService.ts  # Consultation & Treatment operations
│   └── medicineService.ts      # Inventory and stock operations
```

Each service will export functions that make specific Axios calls to their corresponding Django ViewSets (e.g., `patientService.getPatients()` -> `GET /api/patients/`).

---

# Phase 4: Gradual Mock Data Replacement

**Backend-First Integration Rule**: 
Before integrating *any* module into the frontend, the backend must be fully prepared. The following must be confirmed:
1. The **Model exists** in Django.
2. The **Migration is completed** to Supabase PostgreSQL.
3. The **Serializer is verified** for correct JSON outputs, particularly for nested data structures.
4. The **API is tested** locally via Postman or the DRF browsable API.

**Important**: Do NOT delete `mockData.ts` immediately. We will replace modules one by one to ensure the UI does not break.

**Module Replacement Order:**
1. Authentication
2. Users / Roles
3. Patients
4. Consultations
5. Vital Signs
6. Treatments
7. Medicine Inventory
8. Beds, Medical Certificates, Notifications, and other remaining modules

**For each module include:**
- **Backend endpoint required**: Ensure the DRF endpoint is live following the Backend-First Rule.
- **Frontend service required**: Write the corresponding API fetching logic in the `services/` directory.
- **Components affected**: Swap imports in React components from `mockData.ts` to the new API service.
- **Testing steps**: Verify data fetches correctly on component mount and updates accurately upon form submissions.

---

# Phase 5: Data Flow & Contract Validation
- **API Contract Validation**: Ensure the JSON generated by Django serializers perfectly matches the exact structure of the existing TypeScript interfaces in React (e.g., `Patient`, `Consultation`). Adjust Django serializers if necessary to match the frontend, not the other way around.
- **Handle loading states**: Implement visual indicators (spinners/skeletons) while awaiting Axios promises.
- **Handle errors**: Catch 400/500 errors and display user-friendly toast notifications.
- **Validate empty states**: Ensure UI handles empty database tables gracefully (e.g., "No patients found").
- **Prevent UI breaking when API fails**: Add robust fallback UI or generic error boundaries so an API timeout doesn't crash the entire dashboard.

---

# Phase 6: Realtime Preparation (Plan Only)
- **Future Django Channels integration**: Prepare backend for WebSocket connections alongside standard HTTP requests.
- **WebSocket connection structure**: Plan how the frontend will establish and maintain `ws://` connections securely using JWTs.
- **Realtime features**: 
  - Notifications
  - Queue updates
  - Medicine alerts
  - Bed updates
*(Do not implement yet)*

---

# Phase 7: Local Testing Workflow

**How to test locally:**

**Terminal 1:**
Navigate to `cura_backend` and run the Django server:
```bash
python manage.py runserver
```

**Terminal 2:**
Navigate to `cura` and run the React Vite server:
```bash
npm run dev
```

**Verify:**
- Login works and issues a valid JWT using HTTP-only refresh cookies.
- API requests succeed without CORS errors on `localhost:5173`.
- Data displays correctly mapping identically to how mock data rendered.
- Role-based authentication securely restricts unprivileged access to specific components.

**Rules:**
- Keep development local only.
- No Vercel/Render deployment yet.
- Do not remove mock data until API modules are completely stable.
- Build part-by-part.
- Avoid unnecessary complexity.
