# Social Manager

โปรแกรมเดสก์ท็อปสำหรับจัดการและโพสต์ลงหลายเพจ Facebook ที่คุณเป็นแอดมิน ผ่าน Facebook Graph API ทางการ (PySide6)

## ฟีเจอร์หลัก
- เชื่อมต่อหลายเพจ Facebook พร้อมรองรับ Pagination (ดึงได้เกิน 25 เพจ) และปุ่มยกเลิก OAuth
- โพสต์ข้อความ, แนบลิงก์, แนบรูปภาพเดี่ยว และแนบรูปภาพหลายรูปพร้อมกัน
- ตั้งเวลาโพสต์ล่วงหน้าได้ 2 โหมด:
  - **Facebook Native Schedule** (ล่วงหน้า 10 นาที – 75 วัน, ปิดโปรแกรมได้)
  - **คิวในเครื่อง (Local Queue)** (ตั้งเวลาอิสระ, โพสต์อัตโนมัติขณะเปิดโปรแกรม)
- บันทึก **ประวัติการโพสต์** พร้อมกรองสถานะ และปุ่ม **นำกลับมาโพสต์ซ้ำ (Repost)**
- หน้าตั้งค่า `FB_APP_ID` / `FB_APP_SECRET` / Graph API Version ในตัว (เก็บ Token และ Secret ใน OS Keychain)
- รองรับ **โหมดมืด / โหมดสว่าง**, พรีวิวโพสต์แบบเรียลไทม์, นับจำนวนตัวอักษร และเลือก/ไม่เลือกทุกเพจ

## ตั้งค่า Facebook Developer App
1. ไปที่ [developers.facebook.com](https://developers.facebook.com) > My Apps > Create App (ประเภท Business)
2. เพิ่ม Product "Facebook Login for Business" (หรือ Facebook Login)
3. ใน Valid OAuth Redirect URIs ใส่: `http://localhost:8765/callback`
4. คัดลอก App ID และ App Secret (Settings > Basic) ไปใส่ในปุ่ม **⚙ ตั้งค่า** ในโปรแกรม หรือในไฟล์ `.env`

## วิธีติดตั้งและรัน
```powershell
uv venv --python 3.12
uv pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

## รันชุดทดสอบอัตโนมัติ
```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
