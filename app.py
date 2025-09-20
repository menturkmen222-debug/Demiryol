import requests
import json
import time
import logging
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
import random
import threading
import webbrowser
from flask import Flask, render_template_string, request, redirect, session, jsonify
import queue
import math  # 48 soatni 24 soatlik davrlarga bo'lish uchun

# Xatolary aýratyn log faylyna ýazmak
error_handler = logging.FileHandler('otly_bron_ERRORS_ONLY.log', encoding='utf-8')
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

# Asyl loga diňe INFO we DEBUG
info_handler = logging.FileHandler('otly_bron.log', encoding='utf-8')
info_handler.setLevel(logging.DEBUG)
info_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

# Loggeri ýerleşdir
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[info_handler, error_handler, logging.StreamHandler()]
)

# API uç nokdalary
BASE_URL = "https://railway.gov.tm"
TRIPS_ENDPOINT = f"{BASE_URL}/railway-api/trips"
BOOKINGS_ENDPOINT = f"{BASE_URL}/railway-api/bookings"

# Bron başlyklary
HEADERS = {
    "Host": "railway.gov.tm",
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Mobile Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Accept-Encoding": "gzip, deflate",
    "Cookie": "i18n_redirected=tm; vuex=YOUR_VUEX_COOKIE_HERE"
}

# Global o'zgaruvchilar
WAGON_TYPES = [3]  # Plaskart

# Konfigurasiýa
HOLD_TIMEOUT_MIN = 4.5  # Rezerv muddaty 4 daqyqa 30 sekunt
MAX_HELD = 300          # Umumy maksimum bron
MAX_FUTURE_HELD = 50    # 14-15 günlik biletlar üçin maksimum

# Soňky 48 sagat üçin maksimum (foydalanujy girizýär)
MAX_RECENT_HELD = 50   # Default

# Har bir reýs we wagon üçin limitlar
MAX_RECENT_PER_TRIP = 25  # Reýs boýunça maksimum
MAX_RECENT_PER_WAGON = 5  # Wagon boýunça maksimum

held_seats = []  # Held reservations list
passenger_data_storage = []  # Store passenger data with booking codes
ERROR_QUEUE = queue.Queue()  # Xatolary barlamak üçin

# "Kerwen" merkezi üçin qatlary boýunça ýerler
LOWER_BERTHS_1 = ["1", "4", "7", "10", "13", "16", "19", "22", "25", "28", "31", "34", "37", "40", "43", "46", "49", "52", "55", "58"]
LOWER_BERTHS_2 = ["2", "5", "8", "11", "14", "17", "20", "23", "26", "29", "32", "35", "38", "41", "44", "47", "50", "53", "56", "59"]
LOWER_BERTHS_3 = ["3", "6", "9", "12", "15", "18", "21", "24", "27", "30", "33", "36", "39", "42", "45", "48", "51", "54", "57", "60"]
FLOOR_SEATS = {
    "1": LOWER_BERTHS_1,
    "2": LOWER_BERTHS_2,
    "3": LOWER_BERTHS_3
}

# Syýahat gözleg parametrleri
TRIP_PARAMS = {
    "source": "17",  # Aşgabat
    "destination": "27",  # Balkanabat
    "adult": 1,
    "child": 0
}

app = Flask(__name__)
app.secret_key = 'super_secret_key_1234567890'

# Global konfigurasiýa
GLOBAL_CONFIG = {
    'selected_floor': None,  # Default olaraq filtr ýok, UI üçin ulanylar
    'date_filter': None,
    'trip_id_filter': None
}

# Har bir ýer üçin "qutgarmak" threadleri we ony synşyrmak üçin lock
rescue_threads = {}
rescue_lock = threading.Lock()

# Har bir ýer üçin alohida Lock
seat_locks = {}

def determine_gender(surname):
    surname = surname.lower()
    if surname.endswith(('ew', 'w')):
        return 'male'
    elif surname.endswith('wa'):
        return 'female'
    return 'male'

def generate_random_passenger():
    names = ["Allaşükür", "Oraz", "Gurban", "Myrat", "Dovlet", "Nury", "Saparmyrat"]
    surnames = ["Ýowyýew", "Ataye", "Babayew", "Geldiyew", "Hojayew", "Jumayew"]
    name = random.choice(names)
    surname = random.choice(surnames)
    dob_day = random.randint(1, 28)
    dob_month = random.randint(1, 12)
    dob_year = random.randint(1980, 2010)
    dob = f"{dob_day:02d}-{dob_month:02d}-{dob_year}"
    identity_type = "passport"
    identity_number = random.choice(["II-DZ", "I-AG", "I-DZ"]) + " " + str(random.randint(100000, 999999))
    return {
        "has_media_wifi": False,
        "has_lunchbox": False,
        "bedding_type": "default",
        "api_client": "web",
        "contact": {
            "mobile": "+99371789091",
            "email": "menturkmen111@gmail.com",
            "main_contact": f"{name} {surname}"
        },
        "passengers": [
            {
                "name": name,
                "surname": surname,
                "dob": dob,
                "tariff": "adult",
                "gender": determine_gender(surname),
                "identity_type": identity_type,
                "identity_number": identity_number
            }
        ]
    }

def make_request(method, url, data=None, headers=HEADERS, retries=10):
    logging.debug(f"{method} sorag: {url}, Maglumat: {data}")
    session = requests.Session()
    for attempt in range(retries):
        try:
            if method == "POST":
                response = session.post(url, json=data, headers=headers, timeout=20, allow_redirects=True)
            else:
                response = session.get(url, headers=headers, timeout=20, allow_redirects=True)
            logging.debug(f"Status: {response.status_code}, Jogap: {response.text[:500]}...")
            if response.status_code == 200:
                return response
            elif response.status_code == 302:
                location = response.headers.get("Location")
                logging.info(f"Ugrukdyrma: {location}")
                response = session.get(location, headers=headers, timeout=20, allow_redirects=True)
                logging.debug(f"Ugrukdyrma jogaby: Status {response.status_code}, {response.text[:500]}...")
                return response
            elif response.status_code == 429:
                wait_time = 2 ** attempt * 5
                logging.warning(f"429 Çäkden aşyk, {wait_time} sekunt garaşýar...")
                time.sleep(wait_time)
            elif response.status_code in [502, 503, 504]:
                logging.warning(f"Serwer ýalňyşlygy ({response.status_code}), {attempt + 1}/{retries} synanyşyk...")
                time.sleep(5 * (attempt + 1))
            else:
                error_msg = f"Status {response.status_code}: {response.text[:500]}..."
                logging.error(error_msg)
                ERROR_QUEUE.put(error_msg)
                time.sleep(2)
        except requests.RequestException as e:
            error_msg = f"Ýalňyşlyk (Synanyşyk {attempt + 1}/{retries}): {e}"
            logging.error(error_msg)
            ERROR_QUEUE.put(error_msg)
            time.sleep(2 * (attempt + 1))
    logging.error(f"{url} soragy {retries} synanyşykdan soň başa barmady.")
    return None

def search_trips(date):
    logging.info(f"{date} güni otlylary gözleýär...")
    params = TRIP_PARAMS.copy()
    params["date"] = date
    response = make_request("POST", TRIPS_ENDPOINT, params)
    if response and response.json().get("success"):
        trips = response.json().get("data", {}).get("trips", [])
        for trip in trips:
            trip['departure_time'] = trip.get('departure_time', 'N/A')
        return trips
    logging.error("Otly gözleg başa barmady.")
    return []

def get_available_seats(trip_id, wagon_type_id):
    logging.info(f"Otly ID {trip_id} üçin oturgyçlary barlaýar...")
    seat_endpoint = f"{TRIPS_ENDPOINT}/{trip_id}"
    seat_params = {"child": 0, "adult": 1, "outbound_wagon_type_id": wagon_type_id}
    response = make_request("POST", seat_endpoint, seat_params)
    if response and response.json().get("success"):
        outbound = response.json().get("data", {}).get("outbound", {})
        journeys = outbound.get("journeys", [])
        if journeys:
            train_wagons = journeys[0].get("train_wagons", [])
            available_seats = []
            for wagon in train_wagons:
                for seat in wagon.get("seats", []):
                    if seat.get("available"):
                        available_seats.append({
                            "wagon_id": wagon["id"],
                            "seat_id": seat["id"],
                            "seat_number": seat["label"]
                        })
            return available_seats
    logging.error("Boş oturgyç maglumatlaryny almak başa barmady.")
    return []

def book_seat(journey_id, wagon_id, seat_id, passenger_data):
    logging.info(f"Oturgyç bron edýär: Syýahat {journey_id}, Wagon {wagon_id}, Oturgyç ID {seat_id}")
    booking_data = passenger_data.copy()
    booking_data["outbound"] = {
        "selected_journeys": [{
            "id": journey_id,
            "seats": [{"id": seat_id, "train_wagon_id": wagon_id}]
        }]
    }
    seat_lock_key = f"{wagon_id}_{seat_id}"
    if seat_lock_key not in seat_locks:
        seat_locks[seat_lock_key] = threading.Lock()
    with seat_locks[seat_lock_key]:
        response = make_request("POST", BOOKINGS_ENDPOINT, booking_data)
        if response and response.json().get("success"):
            booking = response.json().get("data", {}).get("booking", {})
            payment_url = booking.get("formUrl")
            booking_id = booking.get("id", None)
            if payment_url:
                logging.info(f"Bron üstünlikli! Töleg linki: {payment_url}")
                return payment_url, booking_id, None
            else:
                logging.error("Bron jogabynda payment_url ýok.")
                return None, None, "Bron jogabynda payment_url ýok"
        error_msg = response.text if response else 'Jogap ýok'
        status_code = response.status_code if response else None
        logging.error(f"Bron ýalňyşlygy: {error_msg} (Status: {status_code})")
        if status_code == 409:
            return None, None, "Bu ýer allaqachon bron edilen"
        for retry in range(60):
            time.sleep(0.5)
            logging.info(f"Qayta urinish #{retry + 1} for seat {seat_id}")
            available_seats = get_available_seats(journey_id, WAGON_TYPES[0])
            target_seat = next((s for s in available_seats if s['seat_id'] == seat_id and s['wagon_id'] == wagon_id), None)
            if not target_seat:
                logging.info(f"Ýer {seat_id} artik boş däl, qayta urunmak mümkin däl")
                return None, None, "Ýer artik boş däl"
            response = make_request("POST", BOOKINGS_ENDPOINT, booking_data)
            if response and response.json().get("success"):
                booking = response.json().get("data", {}).get("booking", {})
                payment_url = booking.get("formUrl")
                booking_id = booking.get("id", None)
                if payment_url:
                    logging.info(f"Qayta bron üstünlikli! Töleg linki: {payment_url}")
                    return payment_url, booking_id, None
                else:
                    logging.error("Qayta bron jogabynda payment_url ýok.")
                    break
            error_msg = response.text if response else 'Jogap ýok'
            status_code = response.status_code if response else None
            logging.error(f"Qayta urunmakda ýalňyşlyk: {error_msg} (Status: {status_code})")
            if status_code == 409:
                return None, None, "Bu ýer qayta urunmakda allaqachon bron edilen"
        return None, None, f"Bron etmek näsaz boldy: {error_msg}"
    return None, None, "Näbelli ýalňyşlyk"

def rescue_seat(held):
    """Bron wagty doldan soň, ýeri täzeden bron etmek üçin."""
    try:
        time_to_wait = (held['expiration'] - datetime.now()).total_seconds() - 0.1
        if time_to_wait > 0:
            time.sleep(time_to_wait)
        for attempt in range(60):
            seats = get_available_seats(held['trip_id'], held['wagon_type_id'])
            target_seat = next((s for s in seats if s['seat_id'] == held['seat_id'] and s['wagon_id'] == held['wagon_id']), None)
            if target_seat:
                payment_url, booking_id, error_msg = book_seat(
                    held['journey_id'],
                    held['wagon_id'],
                    held['seat_id'],
                    held['last_book_data']
                )
                if payment_url:
                    with rescue_lock:
                        held['expiration'] = datetime.now() + timedelta(minutes=HOLD_TIMEOUT_MIN)
                        held['booking_id'] = booking_id
                        held['status'] = 'booked'
                        held['error_message'] = None
                    logging.info(f"✅ QUTGARYLDY! Ýer {held['seat_number']} täzeden bron edildi!")
                    start_rescue_thread(held)
                    return
                else:
                    logging.error(f"❌ Synanyşyk #{attempt + 1}: Ýer {held['seat_number']} täzeden bron edip bolmady: {error_msg}")
            else:
                logging.warning(f"⚠️ Synanyşyk #{attempt + 1}: Ýer {held['seat_number']} artik boş däl.")
            time.sleep(0.5)
        logging.error(f"🆘 Ähli 60 synanyşyk näsaz boldy. Ýer {held['seat_number']} roýhatdan aýyrylýar.")
        with rescue_lock:
            if held in held_seats:
                held_seats.remove(held)
            held['status'] = 'error'
            held['error_message'] = "Ähli qayta urunmaklar näsaz boldy. Ýer ýok edildi."
    except Exception as e:
        logging.error(f"🆘 Qutgarmakda näbelli ýalňyşlyk: {str(e)}")
        with rescue_lock:
            held['status'] = 'error'
            held['error_message'] = f"Näbelli ýalňyşlyk: {str(e)}"
            if held in held_seats:
                held_seats.remove(held)

def start_rescue_thread(held):
    """Berlen 'held' obýekdi üçin täzeden rescue thread döret."""
    thread_key = f"{held['trip_id']}_{held['wagon_id']}_{held['seat_id']}"
    if thread_key in rescue_threads:
        del rescue_threads[thread_key]
    thread = threading.Thread(target=rescue_seat, args=(held,), daemon=True)
    rescue_threads[thread_key] = thread
    thread.start()
    logging.info(f"🧵 Ýer {held['seat_number']} üçin rescue thread döredildi. Bron wagty: {held['expiration']}")

def renew_monitor():
    """Diňe 15 günlik limiti gözleg we ýerleri ýok et."""
    while True:
        for held in held_seats[:]:
            now = datetime.now()
            if now > held['start_hold'] + timedelta(days=15):
                with rescue_lock:
                    if held in held_seats:
                        held_seats.remove(held)
                logging.info(f"🗑️  {held['seat_number']} ýeri 15 günden soň roýhatdan aýyryldy")
        time.sleep(60)

import concurrent.futures

def future_dates():
    return [(datetime.now() + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(14, 16)]

def recent_dates():
    return [(datetime.now() + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(0, 2)]

# Yangi funksiýa: sana boýunça 24 soatlyk guruplary almak
def get_24h_period(date_str):
    """Sana bo'yicha 24 soatlyk guruplary almak (0 - bugun, 1 - ertaga)"""
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    today = datetime.now().date()
    delta = (target_date - today).days
    if delta == 0:
        return 0 # Bugun
    elif delta == 1:
        return 1 # Ertaga
    else:
        return None # 48 sagatdan tashqari

# Yangi funksiýa: 48 soatlik limitni 24 soatlik guruhlarga bo'lish
def calculate_24h_limits(total_limit):
    """
    Umumy limiti 24 soatlik guruplara bo'lish.
    Juft son bo'lsa teng bo'linadi, toq son bo'lsa ko'proq qismi birinchi 24 soatga.
    """
    if total_limit < 0:
        raise ValueError("Limit manfiy bo'lishi mumkin emas.")
    # Birinchi 24 soatga ko'proq yoki teng qism (toq sonlarda qolni birinchi guruhga qo'shadi)
    first_24h_limit = math.ceil(total_limit / 2)
    # Qolgan qism ikkinchi 24 soatga
    second_24h_limit = total_limit - first_24h_limit
    return first_24h_limit, second_24h_limit

def monitor_future_dates():
    """14-15 günleri üçin ýerleri gözleg we bron edýär (Diňe 1-nji qatdan)."""
    while True:
        for date in future_dates():
            trips = search_trips(date)
            for trip in trips:
                for wagon_type in WAGON_TYPES:
                    if len(held_seats) >= MAX_HELD:
                        break
                    future_count = len([h for h in held_seats if not h['is_recent']])
                    if future_count >= MAX_FUTURE_HELD:
                        break
                    wagon_data = next((w for w in trip.get("wagon_types", []) if w["wagon_type_id"] == wagon_type and w["has_seats"]), None)
                    if wagon_data:
                        seats = get_available_seats(trip["id"], wagon_type)
                        # DIQQAT: 14-15 GÜNLÜK BILETLERDIŇÝE DIŇE 1-NJI QATDAN ALYŇ
                        seats = [s for s in seats if s["seat_number"] in LOWER_BERTHS_1]
                        to_book = []
                        for seat in seats:
                            if len(held_seats) >= MAX_HELD:
                                break
                            if any(h['seat_id'] == seat['seat_id'] and h['wagon_id'] == seat['wagon_id'] and h['trip_id'] == trip['id'] for h in held_seats):
                                continue
                            passenger = generate_random_passenger()
                            to_book.append({
                                'seat': seat,
                                'passenger': passenger,
                                'journey_id': trip['journeys'][0]['id']
                            })
                            if len(to_book) >= 10:
                                break
                        if to_book:
                            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                                future_to_data = {}
                                for d in to_book:
                                    future = executor.submit(
                                        book_seat,
                                        d['journey_id'],
                                        d['seat']['wagon_id'],
                                        d['seat']['seat_id'],
                                        d['passenger']
                                    )
                                    future_to_data[future] = d
                                for future in concurrent.futures.as_completed(future_to_data):
                                    payment_url, booking_id, error_msg = future.result()
                                    if payment_url:
                                        d = future_to_data[future]
                                        now = datetime.now()
                                        new_held = {
                                            'date': date,
                                            'trip_id': trip['id'],
                                            'journey_id': d['journey_id'],
                                            'wagon_id': d['seat']['wagon_id'],
                                            'seat_id': d['seat']['seat_id'],
                                            'seat_number': d['seat']['seat_number'],
                                            'start_hold': now,
                                            'expiration': now + timedelta(minutes=HOLD_TIMEOUT_MIN),
                                            'last_book_data': d['passenger'],
                                            'wagon_type_id': wagon_type,
                                            'is_recent': False,
                                            'booking_id': booking_id,
                                            'departure_time': trip.get('departure_time', 'N/A'),
                                            'status': 'booked',
                                            'error_message': None
                                        }
                                        with rescue_lock:
                                            held_seats.append(new_held)
                                        logging.info(f"🆕 14-15 GÜNLÜK: Ýer {d['seat']['seat_number']} bron edildi ({date})")
                                        start_rescue_thread(new_held)
                                    else:
                                        logging.error(f"14-15 GÜNLÜK bron näsaz: {error_msg}")
        time.sleep(60)

def monitor_recent_dates():
    """Soňky 48 sagat üçin ýerleri gözleg we bron edýär (ÄHLI QATLARDAN)."""
    # Limitlary hisoblaýan
    max_recent_24h_1, max_recent_24h_2 = calculate_24h_limits(MAX_RECENT_HELD)
    logging.info(f"📊 Soňky 48 sagat üçin limitler: Bugun (0-24h)={max_recent_24h_1}, Ertaga (24-48h)={max_recent_24h_2}")

    while True:
        dates_to_check = recent_dates() # Bugun we ertaga
        # Bugun we ertaga uchun limitlary almak
        period_0_limit = max_recent_24h_1
        period_1_limit = max_recent_24h_2
        period_0_count = len([h for h in held_seats if h['is_recent'] and get_24h_period(h['date']) == 0])
        period_1_count = len([h for h in held_seats if h['is_recent'] and get_24h_period(h['date']) == 1])

        for date in dates_to_check:
            period = get_24h_period(date)
            if period is None:
                continue # 48 sagatdan tashqari

            # Uygun 24 soatlyk limitni almak
            if period == 0:
                max_for_period = period_0_limit
                current_count = period_0_count
            else: # period == 1
                max_for_period = period_1_limit
                current_count = period_1_count

            if current_count >= max_for_period:
                logging.debug(f"24 soatlyk limit (Period {period}) ýeterlik: {current_count}/{max_for_period}")
                continue # Bu 24 soatlyk period üçin limit ýeterlik

            trips = search_trips(date)
            for trip in trips:
                for wagon_type in WAGON_TYPES:
                    if len(held_seats) >= MAX_HELD:
                        break
                    # Period boýunça umumy sanagy täzeden barlaýan
                    if period == 0:
                         current_count = period_0_count = len([h for h in held_seats if h['is_recent'] and get_24h_period(h['date']) == 0])
                    else: # period == 1
                         current_count = period_1_count = len([h for h in held_seats if h['is_recent'] and get_24h_period(h['date']) == 1])
                    if current_count >= max_for_period:
                        logging.debug(f"24 soatlyk limit (Period {period}) ýeterlik: {current_count}/{max_for_period}")
                        break # Bu 24 soatlyk period üçin limit ýeterlik

                    # Reýs boýunça limit (shu 24 soatlyk period üçin)
                    trip_recent_count = len([
                        h for h in held_seats
                        if h['is_recent'] and get_24h_period(h['date']) == period and h['trip_id'] == trip['id']
                    ])
                    if trip_recent_count >= MAX_RECENT_PER_TRIP:
                        logging.debug(f"Reýs üçin limit ýeterlik (Period {period}, Trip {trip['id']}): {trip_recent_count}/{MAX_RECENT_PER_TRIP}")
                        continue

                    wagon_data = next((w for w in trip.get("wagon_types", []) if w["wagon_type_id"] == wagon_type and w["has_seats"]), None)
                    if wagon_data:
                        seats = get_available_seats(trip["id"], wagon_type)
                        # QAVAT FILTRINI AÝYRMAK
                        # Ähli ýerleri barla, selected_floor ulanylmaýar
                        to_book = []
                        for seat in seats:
                            if len(held_seats) >= MAX_HELD:
                                break
                            # Umumy limiti barlaýan
                            if period == 0:
                                current_count = period_0_count = len([h for h in held_seats if h['is_recent'] and get_24h_period(h['date']) == 0])
                            else: # period == 1
                                current_count = period_1_count = len([h for h in held_seats if h['is_recent'] and get_24h_period(h['date']) == 1])
                            if current_count >= max_for_period:
                                logging.debug(f"24 soatlyk limit (Period {period}) ýeterlik: {current_count}/{max_for_period}")
                                break # Bu 24 soatlyk period üçin limit ýeterlik

                            # Yeni: "reserved_for_user" holatyny göz öňünde tutmak
                            if any(h['seat_id'] == seat['seat_id'] and h['wagon_id'] == seat['wagon_id'] and h['trip_id'] == trip['id'] and h.get('status') != 'error' for h in held_seats):
                                continue

                            # Wagon boýunça limit (shu 24 soatlyk period üçin)
                            wagon_recent_count = len([
                                h for h in held_seats
                                if h['is_recent'] and get_24h_period(h['date']) == period and h['trip_id'] == trip['id'] and h['wagon_id'] == seat['wagon_id'] and h.get('status') != 'reserved_for_user'
                            ])
                            if wagon_recent_count >= MAX_RECENT_PER_WAGON:
                                logging.debug(f"Wagon üçin limit ýeterlik (Period {period}, Trip {trip['id']}, Wagon {seat['wagon_id']}): {wagon_recent_count}/{MAX_RECENT_PER_WAGON}")
                                continue

                            passenger = generate_random_passenger()
                            to_book.append({
                                'seat': seat,
                                'passenger': passenger,
                                'journey_id': trip['journeys'][0]['id']
                            })
                            if len(to_book) >= 10:
                                break
                        if to_book:
                            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                                future_to_data = {executor.submit(book_seat, d['journey_id'], d['seat']['wagon_id'], d['seat']['seat_id'], d['passenger']): d for d in to_book}
                                for future in concurrent.futures.as_completed(future_to_data):
                                    payment_url, booking_id, error_msg = future.result()
                                    if payment_url:
                                        d = future_to_data[future]
                                        now = datetime.now()
                                        new_held = {
                                            'date': date,
                                            'trip_id': trip['id'],
                                            'journey_id': d['journey_id'],
                                            'wagon_id': d['seat']['wagon_id'],
                                            'seat_id': d['seat']['seat_id'],
                                            'seat_number': d['seat']['seat_number'],
                                            'start_hold': now,
                                            'expiration': now + timedelta(minutes=HOLD_TIMEOUT_MIN),
                                            'last_book_data': d['passenger'],
                                            'wagon_type_id': wagon_type,
                                            'is_recent': True,
                                            'booking_id': booking_id,
                                            'departure_time': trip.get('departure_time', 'N/A'),
                                            'status': 'booked',
                                            'error_message': None
                                        }
                                        with rescue_lock:
                                            held_seats.append(new_held)
                                            # Sanaglary täzeden barlaýan
                                            if period == 0:
                                                period_0_count += 1
                                            else: # period == 1
                                                period_1_count += 1
                                        logging.info(f"🆕 SOŇKY 48 SAGAT (Period {period}): Ýer {d['seat']['seat_number']} bron edildi ({date})")
                                        start_rescue_thread(new_held)
                                    else:
                                        logging.error(f"Soňky 48 sagatlyk bron näsaz (Period {period}): {error_msg}")
        # Tezlikni oshurmak üçin garaşmak wagty (1 sekunt)
        time.sleep(1)

# HTML ŞABLONLARY (SIZNIŇ KODIŇYZDAN "ÝOLAGÇY MAGLUMATLARY" WE FILTR TIZIMI)
index_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Rezervatsiya Tizimi - "Kerwen" Sagaldyş Merkezi</title>
    <style>
        body {
            background-color: #121212;
            color: #ffffff;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 0;
            padding: 20px;
        }
        h1, h2 {
            text-align: center;
            color: #4CAF50;
        }
        button {
            background: linear-gradient(135deg, #333, #444);
            color: #fff;
            border: none;
            padding: 12px 24px;
            cursor: pointer;
            border-radius: 8px;
            box-shadow: 0 4px 8px rgba(0,0,0,0.3);
            transition: all 0.3s ease;
        }
        button:hover {
            background: linear-gradient(135deg, #444, #555);
            box-shadow: 0 6px 12px rgba(0,0,0,0.5);
        }
        table {
            border-collapse: collapse;
            width: 100%;
            margin-top: 20px;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 4px 8px rgba(0,0,0,0.3);
        }
        th, td {
            border: 1px solid #444;
            padding: 12px;
            text-align: center;
        }
        th {
            background-color: #222;
            color: #4CAF50;
        }
        input, select {
            background-color: #333;
            color: #fff;
            border: 1px solid #444;
            padding: 10px;
            border-radius: 4px;
            margin: 5px 0;
        }
        .section {
            margin-bottom: 40px;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 4px 8px rgba(0,0,0,0.3);
        }
        #recent-section {
            background-color: #1a1a1a;
        }
        #future-section {
            background-color: #121212;
        }
        .status-booked { color: #4CAF50; font-weight: bold; }
        .status-navbatda, .status-izlanyapti { color: #FFEB3B; font-weight: bold; }
        .status-error { color: #F44336; font-weight: bold; }
        ul {
            list-style-type: none;
            padding: 0;
        }
        li {
            background-color: #222;
            margin: 10px 0;
            padding: 15px;
            border-radius: 4px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
        }
        .ticket-info {
            flex: 1;
            min-width: 200px;
        }
        .ticket-actions {
            margin-top: 10px;
        }
        @media (min-width: 768px) {
            .ticket-actions {
                margin-top: 0;
            }
        }
        .kerwen-info {
            background-color: #2a2a2a;
            padding: 15px;
            border-radius: 8px;
            margin: 20px 0;
            text-align: center;
            border: 1px solid #4CAF50;
        }
        .passenger-row {
            background-color: #1a1a1a;
        }
        .passenger-row td {
            text-align: left;
        }
        .status-queue { color: #FFEB3B; } /* Yellow */
        .status-searching { color: #FFEB3B; } /* Yellow */
        .status-found { color: #4CAF50; } /* Green */
        .status-error { color: #F44336; } /* Red */
        .error-btn { cursor: pointer; color: #F44336; }
        .limit-section {
            background-color: #2a2a2a;
            padding: 15px;
            border-radius: 8px;
            margin: 20px 0;
            border: 1px solid #FF9800;
        }
        .limit-section h3 {
            color: #FF9800;
            margin-top: 0;
        }
        .limit-section input {
            width: 80px;
            margin-right: 10px;
        }
        .time-blue { color: #2196F3; font-weight: bold; }
        .time-yellow { color: #FFEB3B; font-weight: bold; }
        .time-red { color: #F44336; font-weight: bold; }
        .time-default { color: #FFFFFF; }
        .period-0 { background-color: #1e3a1e; } /* Tünd ýaşyl */
        .period-1 { background-color: #3a3a1e; } /* Tünd sary */
    </style>
</head>
<body>
    <h1>Rezervatsiya Tizimi - "Kerwen" 200 Orunlyk Sagaldyş Merkezi</h1>
    <div class="kerwen-info">
        <h3>"Kerwen" Sagaldyş Merkezi</h3>
        <p><strong>Umumy 200 orunlyk, 80 otagdan ybarat.</strong></p>
        <p>60 sanysy 2 orunlyk standart otag, 2 sanysy yarym luks otag, 18 sany 4 orunlyk luks ýokary klasly otag.</p>
        <p><strong>Täze VIP Wagonlar!</strong> Raýatlarymyza hödürleýän hyzmatlarymyzyň has hem hilini gowylandyrmak maksady bilen, fewral aýynyň 01-dan VIP Küpe görnüşli wagonlaryň ýola goýulandygyny buýsançly habar berýäris!</p>
        <p>Ýolagçylykda agşamlyk nahary, Ýokary hilli ak ýapynja, Islegiňize görä çaý we kofe, Güýmenjeler üçin Media Wi-Fi</p>
        <p>VIP görnüşdäki hyzmatlar bilen has giňişleýin şu ýerden tanyşyp bilersiňiz.</p>
    </div>

    <div class="limit-section">
        <h3>Soňky 48 Sagat üçin Umumy Limit</h3>
        <form method="post" action="/set_recent_limit">
            Umumy maksimum (48 sagat): <input type="number" name="max_recent_held" value="{{ max_recent_held }}" min="0"><br>
            <button type="submit">Limiti Belläň</button>
        </form>
    </div>

    <div class="section">
        <h2>Stansiýalary Saýlaň</h2>
        <form method="post" action="/set_stations">
            Jo'nash stansiýa ID: <input type="text" name="source" value="{{ trip_params.source }}"><br>
            Yetib borish stansiýa ID: <input type="text" name="destination" value="{{ trip_params.destination }}"><br>
            <button type="submit">Saýla</button>
        </form>
    </div>
    <div class="section">
        <h2>Avtomatik rezervatsiýa (14-15 gün üçin)</h2>
        <form method="post" action="/auto_reserve">
            Gün (YYYY-MM-DD): <input type="text" name="date"><br>
            <button type="submit">Avto rezervatsiýa</button>
        </form>
    </div>
    <div class="section">
        <h2 onclick="togglePassengerData()" style="cursor: pointer;">Ýolagçy maglumatlary ▼</h2>
        <button onclick="clearPassengerData()" style="background: linear-gradient(135deg, #F44336, #D32F2F);">Maglumatlary Tozalamak</button>
        <div id="passenger-data" style="display: none; margin-top: 20px;">
            <table>
                <tr>
                    <th>Tartib</th>
                    <th>Bron kody</th>
                    <th>Link</th>
                    <th>At we Familiýa</th>
                    <th>Ýer</th>
                    <th>Reýs</th>
                    <th>Gün</th>
                    <th>Wagt</th>
                    <th>Holat</th>
                    <th>Xato</th>
                </tr>
                {% for data in passenger_data %}
                <tr class="passenger-row">
                    <td>{{ loop.index }}</td>
                    <td>{{ data.booking_id or 'N/A' }}</td>
                    <td>{% if data.payment_url %}<a href="{{ data.payment_url }}" target="_blank" style="color: #4CAF50;">Toleg</a>{% else %}—{% endif %}</td>
                    <td>{{ data.passenger_data.passengers[0].name }} {{ data.passenger_data.passengers[0].surname }}</td>
                    <td>{{ data.held_data.seat_number if data.held_data else 'N/A' }} (W:{{ data.held_data.wagon_id if data.held_data else 'N/A' }})</td>
                    <td>{{ data.held_data.trip_id if data.held_data else 'N/A' }}</td>
                    <td>{{ data.held_data.date if data.held_data else 'N/A' }}</td>
                    <td>{{ data.timestamp.strftime('%Y-%m-%d %H:%M:%S') }}</td>
                    <td class="status-{{ data.status }}">
                        {% if data.status == 'found' %}
                            ✅ Toplandy
                        {% elif data.status == 'queue' %}
                            ⏳ Navbatda
                        {% elif data.status == 'searching' %}
                            🔍 Izlanyapti
                        {% elif data.status == 'error' %}
                            ❌ Xato
                        {% else %}
                            ❓ Näbelli
                        {% endif %}
                    </td>
                    <td>{% if data.error %}<span class="error-btn" onclick="alert('{{ data.error }}')">Xato</span>{% endif %}</td>
                </tr>
                {% endfor %}
            </table>
        </div>
    </div>
    <div class="section" id="recent-section">
    <h2>Soňky 48 Sagatlyk Biletlar</h2>
    <select id="floor-filter" onchange="filterRecent()">
        <option value="">Qawat saýla</option>
        <option value="1">1-nji (Aşakdaky)</option>
        <option value="2">2-nji</option>
        <option value="3">3-nji (Ýokary)</option>
    </select>
    <select id="date-filter" onchange="filterRecent()">
        <option value="">Gün saýla</option>
        {% for date in recent_dates %}
        <option value="{{ date }}">{{ date }}</option>
        {% endfor %}
    </select>
    <select id="trip-filter" onchange="filterRecent()">
        <option value="">Reýs saýla</option>
        <!-- Dynamically populate via JS -->
    </select>
    <button onclick="checkRecent()">Täze Biletleri Barla</button>
    <div id="recent-tickets">
        <table>
            <tr>
                <th>Tartib</th>
                <th>Gün</th>
                <th>Poýyz wagty</th>
                <th>Reýs ID</th>
                <th>Wagon ID</th>
                <th>Ýer nomeri</th>
                <th>24S Per.</th> <!-- Yangi sütün -->
                <th>Qalan wagt (min)</th>
                <th>Holat</th>
                <th>Hereketler</th>
            </tr>
            <tbody id="recent-tbody">
                <!-- JavaScript bu ýeri doldurar -->
            </tbody>
        </table>
    </div>
</div>
    <div class="section" id="future-section">
        <h2>Rezervlenen ýerler (14-15 gün)</h2>
        <table>
            <tr>
                <th>Tartib</th>
                <th>Gün</th>
                <th>Poýyz wagty</th>
                <th>Reýs ID</th>
                <th>Wagon ID</th>
                <th>Ýer nomeri</th>
                <th>Faol günler</th>
                <th>Qalan günler</th>
                <th>Qalan wagt (min)</th>
                <th>Holat</th>
                <th>Hereketler</th>
            </tr>
            {% for h in held_future %}
            <tr>
                <td>{{ loop.index }}</td>
                <td>{{ h.date }}</td>
                <td>{{ h.departure_time }}</td>
                <td>{{ h.trip_id }}</td>
                <td>{{ h.wagon_id }}</td>
                <td>{{ h.seat_number }}</td>
                <td>{{ (now - h.start_hold).days }}</td>
                <td>{{ (h.start_hold + timedelta(days=15) - now).days }}</td>
                <td>{{ ((h.expiration - now).total_seconds() / 60) | round(2) }}</td>
                <td>
                    {% if h.status == 'booked' %}
                        <span class="status-booked">✅ Bronlandy</span>
                    {% elif h.status == 'error' %}
                        <span class="status-error">❌ Xato</span>
                    {% else %}
                        <span class="status-navbatda">🔍 Izlanyapti</span>
                    {% endif %}
                </td>
                <td>
                    <form method="get" action="/buy/{{ held_seats.index(h) }}">
                        <button type="submit">📝 Bilet satyn al</button>
                    </form>
                    <button onclick="cancelHold({{ held_seats.index(h) }})" style="background: linear-gradient(135deg, #F44336, #D32F2F);">🗑️ Pozmak</button>
                </td>
            </tr>
            {% endfor %}
        </table>
    </div>
    <script>
        function cancelHold(index) {
            if (confirm('Bu bronlamany pozmakçy mysyňyz?')) {
                fetch('/cancel/' + index, {method: 'POST'})
                    .then(() => location.reload());
            }
        }
        function checkRecent() {
            fetch('/check_recent').then(res => res.json()).then(data => {
                window.recentData = data;
                populateTripFilter(data);
                renderRecent(data);
            });
        }
        function populateTripFilter(data) {
            let trips = [...new Set(data.map(item => item.trip_id))];
            let select = document.getElementById('trip-filter');
            select.innerHTML = '<option value="">Reýs saýla</option>';
            trips.forEach(trip => {
                let option = document.createElement('option');
                option.value = trip;
                option.textContent = trip;
                select.appendChild(option);
            });
        }
        function filterRecent() {
    let floor = document.getElementById('floor-filter').value;
    let date = document.getElementById('date-filter').value;
    let trip = document.getElementById('trip-filter').value;
    let filtered = window.recentData.filter(item => {
        let match = true;
        if (floor && floor !== "") {
            if (floor === "1" && !floorSeats["1"].includes(item.seat)) match = false;
            if (floor === "2" && !floorSeats["2"].includes(item.seat)) match = false;
            if (floor === "3" && !floorSeats["3"].includes(item.seat)) match = false;
        }
        if (date && item.date !== date) match = false;
        if (trip && item.trip_id !== trip) match = false;
        return match;
    });
    renderRecent(filtered);
}
        const floorSeats = {{ floor_seats | tojson }};
        function renderRecent(data) {
    const tbody = document.getElementById('recent-tbody');
    tbody.innerHTML = ''; // Eski ma'lumotlary arassala
    if (data.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10" style="text-align: center; color: #aaa;">Boş ýer tapylmady.</td></tr>';
        return;
    }
    data.forEach((item, idx) => {
        let timeClass = item.remaining_min >= 3 ? 'time-blue' : item.remaining_min >= 2 ? 'time-yellow' : item.remaining_min <= 1 ? 'time-red' : 'time-default';
        let statusHtml = '';
        if (item.status === 'booked') {
            statusHtml = '<span class="status-booked">✅ Bronlandy</span>';
        } else if (item.status === 'error') {
            statusHtml = '<span class="status-error">❌ Xato</span>';
        } else if (item.status === 'reserved_for_user') {
            statusHtml = '<span style="color: #FF9800; font-weight: bold;">🔒 Siz üçin saklanýar</span>';
        } else {
            statusHtml = '<span class="status-navbatda">⏳ Navbatda</span>';
        }
        // 24 soatlyk perioda görä renkli fon
        let rowClass = '';
        if (item.period_24h === 0) {
            rowClass = 'period-0'; // Tünd ýaşyl
        } else if (item.period_24h === 1) {
            rowClass = 'period-1'; // Tünd sary
        }

        const row = document.createElement('tr');
        row.className = rowClass; // CSS klassyny goşýan
        row.innerHTML = `
            <td>${idx + 1}</td>
            <td>${item.date}</td>
            <td>${item.departure_time}</td>
            <td>${item.trip_id}</td>
            <td>${item.wagon_id}</td>
            <td>${item.seat}</td>
            <td>${item.period_24h === 0 ? 'Bugun' : item.period_24h === 1 ? 'Ertaga' : 'N/A'}</td> <!-- Yangi sütün -->
            <td class="${timeClass}">${item.remaining_min.toFixed(2)}</td>
            <td>${statusHtml}</td>
            <td>
                <form method="get" action="/buy/${item.index}" style="display: inline;">
                    <button type="submit">📝 Satyn Al</button>
                </form>
            </td>
        `;
        tbody.appendChild(row);
    });
}
        function togglePassengerData() {
            let section = document.getElementById('passenger-data');
            let header = section.previousElementSibling.previousElementSibling;
            if (section.style.display === 'none') {
                section.style.display = 'block';
                header.innerHTML = 'Ýolagçy maglumatlary ▲';
            } else {
                section.style.display = 'none';
                header.innerHTML = 'Ýolagçy maglumatlary ▼';
            }
        }
        function clearPassengerData() {
            if (confirm('Ähli ýolagçy maglumatlaryny tozalamakçy mysyňyz?')) {
                fetch('/clear_passenger_data', {method: 'POST'})
                    .then(() => location.reload());
            }
        }
        // Sahypa açylan wagty, filtrleri real wagtda täzeden barla
        document.addEventListener('DOMContentLoaded', function() {
            checkRecent();
        });
    </script>
</body>
</html>
"""

buy_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Chipta satyn almak</title>
    <style>
        body {
            background-color: #121212;
            color: #ffffff;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 0;
            padding: 20px;
        }
        h1 {
            text-align: center;
            color: #4CAF50;
        }
        .form-container {
            max-width: 600px;
            margin: 0 auto;
            padding: 20px;
            background-color: #1a1a1a;
            border-radius: 8px;
            box-shadow: 0 4px 8px rgba(0,0,0,0.3);
        }
        input, select {
            background-color: #333;
            color: #fff;
            border: 1px solid #444;
            padding: 10px;
            border-radius: 4px;
            width: 100%;
            margin-bottom: 15px;
            box-sizing: border-box;
        }
        button {
            background: linear-gradient(135deg, #333, #444);
            color: #fff;
            border: none;
            padding: 12px 24px;
            cursor: pointer;
            border-radius: 8px;
            box-shadow: 0 4px 8px rgba(0,0,0,0.3);
            transition: all 0.3s ease;
            width: 100%;
        }
        button:hover {
            background: linear-gradient(135deg, #444, #555);
            box-shadow: 0 6px 12px rgba(0,0,0,0.5);
        }
        .timer {
            text-align: center;
            color: #4CAF50;
            font-size: 18px;
            margin-bottom: 20px;
        }
        h2 {
            color: #4CAF50;
            margin-top: 20px;
        }
        .section {
            margin-bottom: 20px;
        }
        .seat-info {
            background-color: #222;
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
            text-align: center;
        }
        .seat-info p {
            margin: 5px 0;
            font-size: 16px;
        }
        .loading-overlay {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.7);
            z-index: 9999;
            justify-content: center;
            align-items: center;
            flex-direction: column;
        }
        .loader {
            border: 8px solid #f3f3f3;
            border-top: 8px solid #4CAF50;
            border-radius: 50%;
            width: 50px;
            height: 50px;
            animation: spin 1s linear infinite;
            margin-bottom: 20px;
        }
        .loading-text {
            color: #fff;
            font-size: 18px;
            margin-bottom: 20px;
        }
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        .home-button {
            background: linear-gradient(135deg, #4CAF50, #45a049);
            margin-top: 20px;
            width: auto;
            padding: 12px 24px;
        }
        .home-button:hover {
            background: linear-gradient(135deg, #45a049, #3e8e41);
        }
    </style>
</head>
<body>
    <h1>Chipta satyn almak</h1>
    <div class="form-container">
        <div class="seat-info">
            <h2>Tanlan ýeriň maglumatlary</h2>
            <p>Gün: {{ held.date }}</p>
            <p>Poýyz wagty: {{ held.departure_time }}</p>
            <p>Reýs ID: {{ held.trip_id }}</p>
            <p>Wagon ID: {{ held.wagon_id }}</p>
            <p>Ýer nomeri: {{ held.seat_number }}</p>
            <p>Holat: 
                {% if held.status == 'booked' %}
                    <span style="color: #4CAF50; font-weight: bold;">✅ Bronlandy</span>
                {% elif held.status == 'error' %}
                    <span style="color: #F44336; font-weight: bold;">❌ Xato: {{ held.error_message }}</span>
                {% elif held.status == 'reserved_for_user' %}
                    <span style="color: #FF9800; font-weight: bold;">🔒 Siz üçin saklanýar</span>
                {% elif held.status == 'queued' %}
                    <span style="color: #FFEB3B; font-weight: bold;">⏳ Navbatda ({{ remaining_min }} min)</span>
                {% else %}
                    <span style="color: #FFEB3B; font-weight: bold;">🔍 Izlanyapti</span>
                {% endif %}
            </p>
        </div>
        <div class="timer">Qalan wagt: <span id="timer">{{ remaining_min }} min</span></div>
        <form method="post" id="booking-form">
            <div class="section">
                <h2>Ýolagçy maglumatlary</h2>
                At: <input name="name" required value="{{ session.get('passenger_data', {}).get('name', '') }}"><br>
                Familiýa: <input name="surname" required value="{{ session.get('passenger_data', {}).get('surname', '') }}"><br>
                Doglan gün (DD-MM-YYYY): <input name="dob" required value="{{ session.get('passenger_data', {}).get('dob', '') }}"><br>
                Şahsyýet belgi nomeri: <input name="identity_number" required value="{{ session.get('passenger_data', {}).get('identity_number', '') }}"><br>
                Telefon: <input name="mobile" required value="{{ session.get('passenger_data', {}).get('mobile', '+99371789091') }}"><br>
                Email: <input name="email" type="email" required value="{{ session.get('passenger_data', {}).get('email', 'menturkmen111@gmail.com') }}"><br>
                Media Portal: <input type="checkbox" name="has_media_wifi" {{ 'checked' if session.get('passenger_data', {}).get('has_media_wifi', False) else '' }}><br>
            </div>
            <button type="submit" onclick="showLoading()">✅ Tassyklamak</button>
            <button type="button" class="home-button" onclick="window.location.href='/'">🏠 Bash Sahypa</button>
        </form>
    </div>
    <div class="loading-overlay" id="loading-overlay">
        <div class="loader"></div>
        <div class="loading-text">Bron edilýär... Garaşyň</div>
        <button class="home-button" onclick="window.location.href='/'">🏠 Bash Sahypa</button>
    </div>
    <script>
        let remainingMin = {{ remaining_min }};
        function updateTimer() {
            if (remainingMin > 0) {
                remainingMin = Math.max(0, remainingMin - 1/60);
                document.getElementById('timer').textContent = remainingMin.toFixed(2) + ' min';
            }
        }
        setInterval(updateTimer, 1000);
        function showLoading() {
            document.getElementById('loading-overlay').style.display = 'flex';
        }
    </script>
</body>
</html>
"""

@app.route('/', methods=['GET'])
def home():
    now = datetime.now()
    if held_seats:
        sorted_held = sorted(held_seats, key=lambda x: datetime.strptime(x['date'], "%Y-%m-%d"))
    else:
        sorted_held = []
    held_recent = [h for h in sorted_held if h['is_recent']]
    held_future = [h for h in sorted_held if not h['is_recent']]
    recent_dates_list = recent_dates()
    return render_template_string(
        index_html,
        held_recent=held_recent,
        held_future=held_future,
        now=now,
        timedelta=timedelta,
        trip_params=TRIP_PARAMS,
        passenger_data=passenger_data_storage,
        session=session,
        held_seats=held_seats,
        recent_dates=recent_dates_list,
        floor_seats=FLOOR_SEATS,
        # Yangi limiti geçirmek
        max_recent_held=MAX_RECENT_HELD
    )

@app.route('/set_stations', methods=['POST'])
def set_stations():
    global TRIP_PARAMS
    TRIP_PARAMS['source'] = request.form['source']
    TRIP_PARAMS['destination'] = request.form['destination']
    return redirect('/')

# Yangi marshrut: Umumy limiti saýlamak
@app.route('/set_recent_limit', methods=['POST'])
def set_recent_limit():
    global MAX_RECENT_HELD
    try:
        new_limit = int(request.form['max_recent_held'])
        if new_limit >= 0:
            MAX_RECENT_HELD = new_limit
            logging.info(f"Umumy limit täzeden bellendi: {MAX_RECENT_HELD}")
        else:
            logging.warning(f"Nädogry limit girizildi: {new_limit}")
    except (ValueError, KeyError) as e:
        logging.error(f"Limiti belläp bolmady: {e}")
    return redirect('/')

@app.route('/set_filters', methods=['POST'])
def set_filters():
    GLOBAL_CONFIG['date_filter'] = request.form.get('date_filter')
    GLOBAL_CONFIG['trip_id_filter'] = request.form.get('trip_id_filter')
    floor_filter = request.form.get('floor_filter', '')
    GLOBAL_CONFIG['selected_floor'] = floor_filter if floor_filter else None  # Boş bolsa None
    return redirect('/')

@app.route('/auto_reserve', methods=['POST'])
def auto_reserve():
    date = request.form['date']
    trips = search_trips(date)
    for trip in trips:
        for wagon_type in WAGON_TYPES:
            if len(held_seats) >= MAX_HELD:
                break
            future_count = len([h for h in held_seats if not h['is_recent']])
            if future_count >= MAX_FUTURE_HELD:
                break
            wagon = next((w for w in trip.get("wagon_types", []) if w["wagon_type_id"] == wagon_type and w["has_seats"]), None)
            if wagon:
                seats = get_available_seats(trip["id"], wagon_type)
                # 14-15 GÜNLÜK BILETLERDIŇÝE DIŇE 1-NJI QATDAN ALYŇ
                seats = [s for s in seats if s["seat_number"] in LOWER_BERTHS_1]
                for seat in seats:
                    if len(held_seats) >= MAX_HELD:
                        break
                    if any(h['seat_id'] == seat['seat_id'] and h['wagon_id'] == seat['wagon_id'] and h['trip_id'] == trip['id'] for h in held_seats):
                        continue
                    passenger = generate_random_passenger()
                    payment_url, booking_id, error_msg = book_seat(trip['journeys'][0]['id'], seat['wagon_id'], seat['seat_id'], passenger)
                    if payment_url:
                        now = datetime.now()
                        new_held = {
                            'date': date,
                            'trip_id': trip['id'],
                            'journey_id': trip['journeys'][0]['id'],
                            'wagon_id': seat['wagon_id'],
                            'seat_id': seat['seat_id'],
                            'seat_number': seat['seat_number'],
                            'start_hold': now,
                            'expiration': now + timedelta(minutes=HOLD_TIMEOUT_MIN),
                            'last_book_data': passenger,
                            'wagon_type_id': wagon_type,
                            'is_recent': False,
                            'booking_id': booking_id,
                            'departure_time': trip.get('departure_time', 'N/A'),
                            'status': 'booked',
                            'error_message': None
                        }
                        with rescue_lock:
                            held_seats.append(new_held)
                        logging.info(f"🆕 Manual reserved seat {seat['seat_number']} for {date}")
                        start_rescue_thread(new_held)
    return redirect('/')

@app.route('/check_recent')
def check_recent():
    # GLOBAL_CONFIG dan filtrleri almak
    date_filter = GLOBAL_CONFIG.get('date_filter')
    trip_id_filter = GLOBAL_CONFIG.get('trip_id_filter')
    selected_floor = GLOBAL_CONFIG.get('selected_floor')  # None ýa-da String
    recent = []
    for h in held_seats:
        if h['is_recent']:  # Diňe 48 sagatlyk bronlary
            match = True
            if date_filter and h['date'] != date_filter:
                match = False
            if trip_id_filter and str(h['trip_id']) != trip_id_filter:
                match = False
            # Qavat filtri (diňe UI üçin)
            if selected_floor is not None and selected_floor != "":
                try:
                    selected_floor_int = int(selected_floor)
                    if selected_floor_int == 1 and h['seat_number'] not in LOWER_BERTHS_1:
                        match = False
                    elif selected_floor_int == 2 and h['seat_number'] not in LOWER_BERTHS_2:
                        match = False
                    elif selected_floor_int == 3 and h['seat_number'] not in LOWER_BERTHS_3:
                        match = False
                    else:
                        logging.warning(f"Nädogry qavat nomeri: {selected_floor}. Ähli ýerler görkezilýär.")
                except (ValueError, TypeError):
                    logging.warning(f"Nädogry qavat nomeri: {selected_floor}. Ähli ýerler görkezilýär.")
            # Eger selected_floor None ýa-da "" bolsa, ähli ýerler görkezilýär
            if match:
                recent.append({
                    'date': h['date'],
                    'seat': h['seat_number'],
                    'index': held_seats.index(h),
                    'departure_time': h.get('departure_time', 'N/A'),
                    'trip_id': h['trip_id'],
                    'wagon_id': h['wagon_id'],
                    'remaining_min': round((h['expiration'] - datetime.now()).total_seconds() / 60, 2),
                    'status': h.get('status', 'searching'),
                    'error_message': h.get('error_message', ''),
                    'period_24h': get_24h_period(h['date']) # Yangi maglumat
                })
    return jsonify(recent)

@app.route('/buy/<int:index>', methods=['GET', 'POST'])
def buy(index):
    try:
        held = held_seats[index]
    except IndexError:
        logging.error(f"Invalid index: {index}")
        return render_template_string("""
            <h1 style='color: #F44336; text-align: center;'>Ýalňyşlyk</h1>
            <p style='text-align: center;'>Saýlanan oturgyç tapylmady!</p>
            <a href="/" style='display: block; text-align: center; color: #4CAF50;'>Baş sahypa gaýt</a>
        """)
    remaining_min = round((held['expiration'] - datetime.now()).total_seconds() / 60, 2)
    if request.method == 'GET':
        return render_template_string(buy_html, remaining_min=remaining_min, session=session, held=held)
    
    # with threading.Lock(): BUNY AYYRMAK KERK - endi bu blok ýok
    passenger = {
        "has_media_wifi": request.form.get('has_media_wifi') == 'on',
        "has_lunchbox": False,
        "bedding_type": "default",
        "api_client": "web",
        "contact": {
            "mobile": request.form['mobile'],
            "email": request.form['email'],
            "main_contact": f"{request.form['name']} {request.form['surname']}"
        },
        "passengers": [
            {
                "name": request.form['name'],
                "surname": request.form['surname'],
                "dob": request.form['dob'],
                "tariff": "adult",
                "gender": determine_gender(request.form['surname']),
                "identity_type": "passport",
                "identity_number": request.form['identity_number']
            }
        ]
    }
    session['passenger_data'] = passenger
    
    # Birinji: Ýeri foydalanujy üçin "qulplamak"
    with rescue_lock:
        if held.get('status') == 'reserved_for_user':
            # Eger biri tarapyndan ulanylýan bolsa
            return render_template_string("""
                <h1 style='color: #F44336; text-align: center;'>Ýer öýledip bilinmedi</h1>
                <p style='text-align: center;'>Bu ýer biri tarapyndan satyn alynýan. Täzeden synanyşyň.</p>
                <a href="/" style='display: block; text-align: center; color: #4CAF50;'>Baş sahypa gaýt</a>
            """)
        # Holaty üýtgetmek
        held['status'] = 'reserved_for_user'
        held['user_passenger_data'] = passenger # Foydalanujynyň maglumatlaryny saklamak
    
    def attempt_booking():
        now = datetime.now()
        status = 'queue' if now < held['expiration'] else 'searching'
        entry = {
            'booking_id': held['booking_id'],
            'passenger_data': passenger,
            'timestamp': now,
            'status': status,
            'error': None,
            'payment_url': None,
            'held_data': {
                'seat_number': held['seat_number'],
                'wagon_id': held['wagon_id'],
                'trip_id': held['trip_id'],
                'date': held['date'],
                'status': status
            }
        }
        passenger_data_storage.append(entry)
        
        if now < held['expiration']:
            time_to_wait = (held['expiration'] - now).total_seconds() + 0.1
            logging.info(f"Garaşylýar {time_to_wait} sekunt, rezerwasyaň wagty doldy")
            time.sleep(time_to_wait)
        
        for attempt in range(60):
            seats = get_available_seats(held['trip_id'], held['wagon_type_id'])
            target_seat = next((s for s in seats if s['seat_id'] == held['seat_id'] and s['wagon_id'] == held['wagon_id']), None)
            if target_seat:
                # Foydalanujynyň öz ma'lumatlary bilen bron etmek
                payment_url, booking_id, error_msg = book_seat(held['journey_id'], held['wagon_id'], held['seat_id'], passenger)
                if payment_url:
                    entry['status'] = 'found'
                    entry['payment_url'] = payment_url
                    entry['booking_id'] = booking_id
                    entry['held_data']['status'] = 'found'
                    with rescue_lock:
                        held['last_book_data'] = passenger
                        held['expiration'] = datetime.now() + timedelta(minutes=HOLD_TIMEOUT_MIN)
                        held['booking_id'] = booking_id
                        held['status'] = 'booked'
                        held['error_message'] = None
                        # user_passenger_data yzyna aýdarma
                        if 'user_passenger_data' in held:
                            del held['user_passenger_data']
                    try:
                        webbrowser.open(payment_url)
                    except:
                        logging.error("Webbrowser açylyp bilmedi")
                    start_rescue_thread(held)
                    return render_template_string("""
                        <h1 style='color: #4CAF50; text-align: center;'>Bron üstünlikli!</h1>
                        <p style='text-align: center;'>Bron kody: <strong>{{ booking_id }}</strong></p>
                        <p style='text-align: center;'>Töleg linki: <a href="{{ payment_url }}" target="_blank">{{ payment_url }}</a></p>
                        <a href="/" style='display: block; text-align: center; color: #4CAF50;'>Baş sahypa gaýt</a>
                        <script>
                            if (window.parent && window.parent.document.getElementById('loading-overlay')) {
                                window.parent.document.getElementById('loading-overlay').style.display = 'none';
                            }
                        </script>
                    """, booking_id=booking_id, payment_url=payment_url)
                else:
                    entry['status'] = 'error'
                    entry['error'] = error_msg
                    entry['held_data']['status'] = 'error'
                    with rescue_lock:
                        held['status'] = 'error'
                        held['error_message'] = error_msg
                        # user_passenger_data yzyna aýdarma
                        if 'user_passenger_data' in held:
                            del held['user_passenger_data']
                    break
            else:
                logging.info(f"Ýer {held['seat_number']} elýeterli däl, täzeden synanýar...")
            time.sleep(0.5)
        
        # Eger ähli synanyşyklar näsaz bolsa
        entry['status'] = 'error'
        entry['error'] = 'Ýer tapylmady ýa-da bron etmek näsaz boldy'
        entry['held_data']['status'] = 'error'
        with rescue_lock:
            held['status'] = 'error'
            held['error_message'] = entry['error']
            # user_passenger_data yzyna aýdarma
            if 'user_passenger_data' in held:
                del held['user_passenger_data']
        
        return render_template_string("""
            <h1 style='color: #F44336; text-align: center;'>Bron etmek näsaz boldy</h1>
            <p style='text-align: center;'>{{ error_message }}</p>
            <a href="/" style='display: block; text-align: center; color: #4CAF50;'>Baş sahypa gaýt</a>
            <script>
                if (window.parent && window.parent.document.getElementById('loading-overlay')) {
                    window.parent.document.getElementById('loading-overlay').style.display = 'none';
                }
            </script>
        """, error_message=entry['error'])
    
    try:
        return attempt_booking()
    except Exception as e:
        logging.error(f"Booking error: {str(e)}")
        # Xatolary barlamak we holady yzyna aýdarmak
        with rescue_lock:
            held['status'] = 'error'
            held['error_message'] = str(e)
            if 'user_passenger_data' in held:
                del held['user_passenger_data']
        return render_template_string("""
            <h1 style='color: #F44336; text-align: center;'>Bron ýalňyşlygy</h1>
            <p style='text-align: center;'>Näbelli xato ýüze çykdy: {{ error }}</p>
            <a href="/" style='display: block; text-align: center; color: #4CAF50;'>Baş sahypa gaýt</a>
            <script>
                if (window.parent && window.parent.document.getElementById('loading-overlay')) {
                    window.parent.document.getElementById('loading-overlay').style.display = 'none';
                }
            </script>
        """, error=str(e))

@app.route('/cancel/<int:index>', methods=['POST'])
def cancel(index):
    try:
        with rescue_lock:
            held = held_seats[index]
            logging.info(f"Pozulýar: ýer {held['seat_number']} {held['date']} üçin")
            del held_seats[index]
        return jsonify({"success": True})
    except IndexError:
        logging.error(f"Pozmak üçin nädogry indeks: {index}")
        return jsonify({"success": False, "error": "Ýer tapylmady"}), 404

@app.route('/clear_passenger_data', methods=['POST'])
def clear_passenger_data():
    global passenger_data_storage
    passenger_data_storage = []
    logging.info("Ähli ýolagçy maglumatlary tozalandy")
    return jsonify({"success": True})

if __name__ == "__main__":
    # Monitor threadleri
    renew_thread = threading.Thread(target=renew_monitor, daemon=True)
    renew_thread.start()
    # 14-15 günleri üçin monitor
    future_thread = threading.Thread(target=monitor_future_dates, daemon=True)
    future_thread.start()
    # Soňky 48 sagat üçin monitor
    recent_thread = threading.Thread(target=monitor_recent_dates, daemon=True)
    recent_thread.start()
    # Flask serveri
    print("\n🚀 REZERVATSIÝA TIZIMI ISHLEÝÄR!")
    print("🌐 Brauzeriňizde şu manzalary synanyşyň:")
    print("   → http://localhost:5000")
    print("   → http://127.0.0.1:5000")
    app.run(debug=True, host='0.0.0.0', port=5000)