# One-Click OAuth Backend สำหรับ Social Manager

เซิร์ฟเวอร์ขนาดเล็กสำหรับให้ผู้ใช้ปลายทางของ `Social Manager` กด **"เชื่อมต่อ Facebook"** ได้เลยในคลิกเดียว โดย **ไม่ต้องสร้าง Meta Developer App และไม่ต้องกรอก App ID / App Secret เอง**

## ขั้นตอน Deploy (เลือกใช้ Vercel, Render หรือ Railway)

1. ตั้งค่า Environment Variables บนโฮสต์ของคุณ:
   - `FB_APP_ID` = App ID จาก Meta Developer
   - `FB_APP_SECRET` = App Secret จาก Meta Developer (เก็บไว้บน Server เท่านั้น)
   - `PUBLIC_BASE_URL` = URL ของ Server คุณ เช่น `https://social-manager-auth.vercel.app`
   - `SERVER_SIGNING_KEY` = สตริงสุ่มยาวๆ 32+ ตัวอักษร (เช่น รัน `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
2. ใน Meta Developer > **Facebook Login > Settings > Valid OAuth Redirect URIs** ให้เพิ่ม:
   - `https://<โดเมนของคุณ>/api/fb/callback`
3. (กรณีแจกให้คนอื่นใช้งานด้วย) ส่งขอ **Meta App Review** สำหรับสิทธิ์ `pages_show_list`, `pages_manage_posts`, `pages_read_engagement`, `pages_manage_engagement`, `read_insights` และสลับแอปเป็นโหมด **Live**
4. ในโปรแกรม `Social Manager`:
   - เปิด **ตั้งค่า** > แท็บ **2. One-Click Backend**
   - ติ๊ก **เปิดใช้โหมด One-Click ผ่าน Backend Server**
   - วาง URL เช่น `https://<โดเมนของคุณ>` > กด **ทดสอบการเชื่อมต่อ Server** > กด **บันทึกการตั้งค่า**

## รันทดสอบในเครื่อง (Local)
```powershell
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8000
```
จากนั้นลองเปิด `http://127.0.0.1:8000/api/health`
