<?php
// เริ่มเซสชัน
session_start();

// ตรวจสอบว่าผู้ใช้ได้เข้าสู่ระบบหรือไม่
if (!isset($_SESSION['userId']) || !isset($_SESSION['username'])) {
    // ถ้ายังไม่ได้เข้าสู่ระบบ ให้ส่งกลับไปที่หน้า login
    header("Location: login.php");
    exit();
}

// ดึงข้อมูลจากเซสชัน
$userId = $_SESSION['userId'];
$username = $_SESSION['username'];
$password = $_SESSION['password'];
?>

<!DOCTYPE html>
<html lang="th">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>選擇伺服器。</title>
</head>
<body>
    <h1>選擇伺服器。</h1>

    <!-- 選擇伺服器的表單 -->
    <form action="game.php" method="GET">
        <label for="server">選擇伺服器。:</label>
        <select name="serverId" id="server" required>
            <option value="1">伺服器 1</option>
            <!-- 在此處新增其他伺服器 -->
        </select>
        <br><br>
        
        <!-- ส่งข้อมูลผู้ใช้ไปที่ game.php -->
        <input type="hidden" name="userId" value="<?php echo $userId; ?>">
        <input type="hidden" name="username" value="<?php echo $username; ?>">
        <input type="hidden" name="password" value="<?php echo $password; ?>">

        <input type="submit" value="開始遊戲。">
    </form>
</body>
</html>
