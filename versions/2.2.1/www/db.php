<?php
// db.php - การเชื่อมต่อฐานข้อมูล MySQL

$servername = "localhost";
$username = "root";
$password = "vertrigo";
$dbname = "game_db";

// สร้างการเชื่อมต่อ
$conn = new mysqli($servername, $username, $password, $dbname);

// ตรวจสอบการเชื่อมต่อ
if ($conn->connect_error) {
    die("Connection failed: " . $conn->connect_error);
}
?>
