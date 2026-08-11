<?php

// รวมไลบรารี password_compat (เพิ่มเส้นทางที่ถูกต้องในไฟล์ของคุณ)
require_once '/lib/password.php';  // ใส่ที่อยู่ของไฟล์ที่คุณดาวน์โหลดและแตกไฟล์ไว้
// รวมการเชื่อมต่อฐานข้อมูล
include('db.php');

// ฟังก์ชันสำหรับสมัครสมาชิก
function register($username, $password, $email) {
    global $conn;
    
    // ตรวจสอบว่า username ซ้ำหรือไม่
    $sql = "SELECT * FROM accounts WHERE username='$username'";
    $result = $conn->query($sql);
    
    if ($result->num_rows > 0) {
        return "重複使用者名稱!";
    } else {
        // เข้ารหัสรหัสผ่าน
        $hashed_password = password_hash($password, PASSWORD_DEFAULT);
        
        // เพิ่มข้อมูลลงในฐานข้อมูล
        $sql = "INSERT INTO accounts (username, password, email, status, created_at)
                VALUES ('$username', '$hashed_password', '$email', 1, NOW())";
                
        if ($conn->query($sql) === TRUE) {
            return "註冊成功。!";
        } else {
            return "發生錯誤。: " . $conn->error;
        }
    }
}

// ตรวจสอบการส่งข้อมูลจากฟอร์ม
if ($_SERVER['REQUEST_METHOD'] == 'POST') {
    if (isset($_POST['register'])) {
        // รับข้อมูลจากฟอร์มสมัครสมาชิก
        $username = $_POST['username'];
        $password = $_POST['password'];
        $email = $_POST['email'];
        
        $message = register($username, $password, $email);
    }
}
?>

<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>登記</title>
</head>
<body>
    <h1>登記</h1>
    <form action="register.php" method="POST">
        <label for="username">使用者名稱:</label>
        <input type="text" id="username" name="username" required><br><br>
        
        <label for="password">密碼:</label>
        <input type="password" id="password" name="password" required><br><br>
        
        <label for="email">電子郵件:</label>
        <input type="email" id="email" name="email"><br><br>
        
        <input type="submit" name="register" value="สมัครสมาชิก">
    </form>

    <?php
    // แสดงผลลัพธ์จากการสมัครสมาชิก
    if (isset($message)) {
        echo "<p>$message</p>";
    }
    ?>
</body>
</html>
