from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
import psycopg2
from psycopg2.extras import RealDictCursor
import pandas as pd
import io
from typing import List

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db_connection():
    return psycopg2.connect(
        host="localhost",
        database="database_hctn", 
        user="postgres",
        password="12345",
        port="5432"
    )

#  клиенты
@app.get("/api/clients")
def get_clients():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        query = """
            SELECT 
                c.client_id, c.full_name, c.phone, c.email, c.loyalty_tier, c.registration_date,
                c.share_history_allowed,
                v.visit_timestamp, v.amount_spent, v.duration_minutes,
                vn.venue_name, vn.category
            FROM clients c
            LEFT JOIN visits v ON c.client_id = v.client_id
            LEFT JOIN venues vn ON v.venue_id = vn.venue_id
            ORDER BY c.client_id
        """
        cur.execute(query)
        rows = cur.fetchall()
        
        clients_dict = {}
        for row in rows:
            cid = row['client_id']
            if cid not in clients_dict:
                # ЛОГИКА СКЛЕЙКИ
                score = 60
                if row['phone'] and row['email']:
                    score = 100
                elif row['phone'] or row['email']:
                    score = 95
                
                # Форматируем дату в читаемый вид (например, 15.01.2023)
                reg_date = row['registration_date'].strftime('%d.%m.%Y') if row['registration_date'] else 'Неизвестно'
                
                # Приводим статус к красивому виду (Gold, Silver, Platinum, Basic)
                tier_raw = str(row['loyalty_tier'] or 'Basic').strip().capitalize()
                
                clients_dict[cid] = {
                    "id": str(cid),
                    "name": row['full_name'],
                    "phone": row['phone'],
                    "email": row['email'],
                    "loyalty_tier": tier_raw, # <-- Красивый статус
                    "registration_date": reg_date,
                    "share_history": row['share_history_allowed'],
                    "match": f"{score}%",
                    "history": []
                }
            if row['visit_timestamp']:
                date_str = row['visit_timestamp'].strftime('%d %b')
                clients_dict[cid]["history"].append({
                    "time": date_str,
                    "place": row['category'],
                    "amount": float(row['amount_spent'])
                })
        return list(clients_dict.values())
    finally:
        cur.close()
        conn.close()

# загрузка файлов
@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...), source_type: str = Form(...)):
    contents = await file.read()
    
    try:
        if file.filename.endswith('.xlsx') or file.filename.endswith('.xls'):
            df = pd.read_excel(io.BytesIO(contents))
        elif file.filename.endswith('.csv'):
            df = pd.read_csv(io.BytesIO(contents))
        else:
            return {"error": "Неподдерживаемый формат файла. Нужен CSV или XLSX"}
        
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        added_count = 0
        
        for index, row in df.iterrows():
            phone = str(row['phone']).strip()
            if phone.endswith('.0'): phone = phone[:-2]
            
            name = str(row['name']).strip()
            place = str(row['place']).strip() 
            amount = float(row['amount'])
            
            # данные из базы
            email = str(row['email']).strip() if 'email' in row and pd.notna(row['email']) else None
            
            tier = 'Basic'
            if 'loyalty_tier' in row and pd.notna(row['loyalty_tier']):
                tier = str(row['loyalty_tier']).strip().capitalize()
            
            # правка даты регистрации
            if 'registration_date' in row and pd.notna(row['registration_date']):
                reg_date = pd.to_datetime(row['registration_date']).strftime('%Y-%m-%d')
            else:
                reg_date = None
            
            share_hist = bool(row['share_history_allowed']) if 'share_history_allowed' in row and pd.notna(row['share_history_allowed']) else True

            cur.execute("SELECT client_id FROM clients WHERE phone = %s", (phone,))
            client = cur.fetchone()
            
            if not client:
                if reg_date:
                    cur.execute(
                        """INSERT INTO clients (full_name, phone, email, loyalty_tier, registration_date, share_history_allowed) 
                           VALUES (%s, %s, %s, %s, %s, %s) RETURNING client_id""",
                        (name, phone, email, tier, reg_date, share_hist)
                    )
                else:
                    cur.execute(
                        """INSERT INTO clients (full_name, phone, email, loyalty_tier, registration_date, share_history_allowed) 
                           VALUES (%s, %s, %s, %s, NOW(), %s) RETURNING client_id""",
                        (name, phone, email, tier, share_hist)
                    )
                client_id = cur.fetchone()['client_id']
            else:
                client_id = client['client_id']
            
            cur.execute("SELECT venue_id FROM venues WHERE category = %s LIMIT 1", (place,))
            venue = cur.fetchone()
            
            if not venue:
                cur.execute(
                    "INSERT INTO venues (venue_name, category) VALUES (%s, %s) RETURNING venue_id",
                    (place, place)
                )
                venue_id = cur.fetchone()['venue_id']
            else:
                venue_id = venue['venue_id']
            
            cur.execute(
                "INSERT INTO visits (receipt_number, client_id, venue_id, visit_timestamp, amount_spent) VALUES (%s, %s, %s, NOW(), %s)",
                (f"CHK-{index}-{phone}", client_id, venue_id, amount)
            )
            added_count += 1
            
        conn.commit()
        cur.close()
        conn.close()
        return {"message": f"Успешно загружено {added_count} записей"}
        
    except Exception as e:
        return {"error": str(e)}

# удаление клиента
@app.delete("/api/clients/{client_id}")
def delete_client(client_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM visits WHERE client_id = %s", (client_id,))
        cur.execute("DELETE FROM clients WHERE client_id = %s", (client_id,))
        conn.commit()
        return {"message": "Клиент удален"}
    except Exception as e:
        conn.rollback()
        return {"error": str(e)}
    finally:
        cur.close()
        conn.close()

# удаление большого кол-ва клиентов
@app.post("/api/clients/delete_batch")
def delete_clients_batch(client_ids: List[int]):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        if not client_ids:
            return {"message": "Нет клиентов для удаления"}
            
        for c_id in client_ids:
            cur.execute("DELETE FROM visits WHERE client_id = %s", (c_id,))
            cur.execute("DELETE FROM clients WHERE client_id = %s", (c_id,))
        
        conn.commit()
        return {"message": f"Успешно удалено клиентов: {len(client_ids)}"}
    except Exception as e:
        conn.rollback()
        return {"error": str(e)}
    finally:
        cur.close()
        conn.close()
