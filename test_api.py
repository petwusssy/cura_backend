import requests

def run_tests():
    print('1. Testing Backend Health Endpoint...')
    try:
        health = requests.get('http://localhost:8000/api/health/')
        print(f'Status: {health.status_code}, Response: {health.json()}')
    except Exception as e:
        print(f'Health Check Failed: {e}')
        return

    print('\n2. Testing Authentication & JWT...')
    login_data = {'username': 'admin', 'password': 'password123'}
    try:
        login = requests.post('http://localhost:8000/api/token/', json=login_data)
        print(f'Status: {login.status_code}')
        if login.status_code == 200:
            token = login.json().get('access')
            print('Access token retrieved successfully.')
        else:
            print(f'Login Failed: {login.text}')
            return
    except Exception as e:
        print(f'Login Check Failed: {e}')
        return

    print('\n3. Testing Protected Endpoint (GET /api/patients/)...')
    headers = {'Authorization': f'Bearer {token}'}
    try:
        patients = requests.get('http://localhost:8000/api/patients/', headers=headers)
        print(f'Status: {patients.status_code}')
        if patients.status_code == 200:
            print(f'Successfully fetched patients: {patients.json()}')
        else:
            print(f'Failed to fetch patients: {patients.text}')
    except Exception as e:
        print(f'Patient Check Failed: {e}')

    print('\n4. Testing CORS Headers (OPTIONS /api/patients/)...')
    cors_headers = {'Origin': 'http://localhost:5173', 'Access-Control-Request-Method': 'GET'}
    try:
        cors = requests.options('http://localhost:8000/api/patients/', headers=cors_headers)
        print(f'Status: {cors.status_code}')
        print(f'Access-Control-Allow-Origin: {cors.headers.get("Access-Control-Allow-Origin", "Missing")}')
        print(f'Access-Control-Allow-Credentials: {cors.headers.get("Access-Control-Allow-Credentials", "Missing")}')
    except Exception as e:
        print(f'CORS Check Failed: {e}')

run_tests()
