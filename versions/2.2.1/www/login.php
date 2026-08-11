
<?php
// เริ่มเซสชัน
session_start();

// รวมไลบรารี password_compat (ใส่เส้นทางที่ถูกต้องของไฟล์ password.php ที่คุณดาวน์โหลดมา)
require_once '/lib/password.php';  // ใส่เส้นทางให้ถูกต้อง
// การเชื่อมต่อฐานข้อมูล
include('db.php');

// ฟังก์ชันสำหรับการล็อกอิน
function login($username, $password) {
    global $conn;
    
    // ตรวจสอบข้อมูลผู้ใช้จากฐานข้อมูล
    $sql = "SELECT * FROM accounts WHERE username='$username'";
    $result = $conn->query($sql);
    
    if ($result->num_rows > 0) {
        $row = $result->fetch_assoc();
        // ตรวจสอบรหัสผ่านโดยใช้ password_verify()
        if (password_verify($password, $row['password'])) {
            // ตั้งค่า session สำหรับ userId, username และ password
            $_SESSION['userId'] = $row['id'];
            $_SESSION['username'] = $row['username'];
            $_SESSION['password'] = $row['password'];

            // ส่งผู้ใช้ไปที่หน้า index.php เพื่อให้เลือกเซิร์ฟเวอร์
            header("Location: index.php");
            exit();
        } else {
            return "密碼錯誤。!";
        }
    } else {
        return "未找到用戶。!";
    }
}

// ตรวจสอบการส่งข้อมูลจากฟอร์ม
if ($_SERVER['REQUEST_METHOD'] == 'POST') {
    if (isset($_POST['login'])) {
        // รับข้อมูลจากฟอร์มล็อกอิน
        $username = $_POST['username'];
        $password = $_POST['password'];
        
        $message = login($username, $password);
    }
}
?>

<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>登入</title>
</head>
<body>
    <h1>登入</h1>
    <form action="login.php" method="POST">
        <label for="username">使用者名稱:</label>
        <input type="text" id="username" name="username" required><br><br>
        
        <label for="password">密碼:</label>
        <input type="password" id="password" name="password" required><br><br>
        
        <input type="submit" name="login" value="登入">
    </form>

    <?php
    // แสดงผลลัพธ์จากการล็อกอิน
    if (isset($message)) {
        echo "<p>$message</p>";
    }
    ?>
</body>
</html>

