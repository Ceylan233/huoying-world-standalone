<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN" "http://www.w3.org/TR/html4/loose.dtd">
<html>
<head>
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">
<title>火影世界公益服</title>
<!-- <body>
<object width="600" height="600" classid="clsid:D27CDB6E-AE6D-11cf-96B8-444553540000" codebase="http://download.macromedia.com/pub/shockwave/cabs/flash/swflash.cab#4,0,0,0">
<param name="src" value="clock.swf" />
<param name="quality" value="high" />
<embed src="expressinstall.swf" type="application/x-shockwave-flash" width="600" height="600" quality="high" pluginspage="http://www.macromedia.com/go/getflashplayer"></embed>
</object>
</body> -->
<style type="text/css">
* {
    margin: 0;
    padding: 0;
    border: 0;
    overflow: hidden;
    background-color: #ffffff;
}

html,body,#flashContainer,#flash {
    height: 100%;
}

body {
    text-align: center;
}
</style>
<script type="text/javascript" src="/swfobject2.js"></script>
<script language="javascript">
      // var myFlash = (function(){
        
      //   if(typeof window.ActiveXObject != "undefined"){
      //     return new ActiveXObject("ShockwaveFlash.ShockwaveFlash");
      //   }else{
      //     console.log(11111)
      //     return navigator.plugins['Shockwave Flash'];
      //   }
      // })();
//判断是否开启了flash
 function flashChecker () {
    var hasFlash = 0; //是否安装了flash
    var flashVersion = 0; //flash版本
    var isIE = /*@cc_on!@*/ 0; //是否IE浏览器
    if (isIE) {
        var swf = new ActiveXObject('ShockwaveFlash.ShockwaveFlash');
        if (swf) {
            hasFlash = 1;
            VSwf = swf.GetVariable("$version");
            flashVersion = parseInt(VSwf.split(" ")[1].split(",")[0]);
        }
    } else {
        if (navigator.plugins && navigator.plugins.length > 0) {
            var swf = navigator.plugins["Shockwave Flash"];
            if (swf) {
                hasFlash = 1;
                var words = swf.description.split(" ");
                for (var i = 0; i < words.length; ++i) {
                    if (isNaN(parseInt(words[i]))) continue;
                    flashVersion = parseInt(words[i]);
                }
            }
        }
    }
    return {
        f: hasFlash,
        v: flashVersion
    };
}
function wgq(){
	var fls = flashChecker();
//	console.log(fls)
	if (!fls.f) {
		
		var a = document.getElementById('a');
		a.click();

	
	}
      
}
  
  function log(msg) {
    try {
      console.log(msg);
    }
    catch (e) { }
  }

  function printObj(obj) {
    try {
      for (var k in obj) { 
    //    log(" " + k + " = " + obj[k]); 
      }
    } catch (e) {}
  }

  function embedFlash(params, mainswf) {
    var settings = {menu: "false",allowFullScreen:"false",allowScriptAccess:"always",wmode:"window",bgcolor:"#000000"};
    var attributes = {};
    swfobject.embedSWF(mainswf, "flash", "100%", "100%", "10.0.0", "expressinstall.swf" ,params, settings, attributes);
  }

  //log(location.host + ", " + location.pathname + ", " + location.search);
  //log(ua);
  // default value
  var lanHost = window.location.hostname || '127.0.0.1';
  var lanHttpHost = window.location.host || lanHost;
  // 客户端必需参数（从loaderInfo.parameters读取）
  var params = { 
	// 核心连接参数（必需）
	ip:lanHost,      // TCP服务器IP
	port:'18684',         // TCP服务器端口
	baseUrl:lanHttpHost + '/act_web_tiyan', // 资源文件基础URL，客户端会自动加上"http://"前缀
	                                   // 注意：客户端会从 http://127.0.0.1/act_web_tiyan/asset/MainLoginServer.swf 加载登录模块
	
	// 自动登录参数（可选，如果提供userName则自动登录）
	userName:'Dieu',     // 用户名
	serverId:'1',        // 服务器ID
	userId:'1',          // 用户ID
	time:'',             // 时间戳
	sign:'',            // 签名
	password:'test123',         // 密码（可选）
	username:'',         // 用户名（可选，与userName重复）
	
	// 游戏配置参数（客户端会读取，使用默认值或传递的值）
	partner:'gongyi',           // 合作伙伴标识
	forbidXiaoFei:'0',          // 禁止消费
	fightNotice:'0',            // 战斗通知
	useParnterLogo:'0',         // 使用合作伙伴Logo
	ybRatio:'1U=70元寶',        // 元宝比例
	gameSwitch:'lunhuiyan:1_kunchong:1_jieyin:1_zhongrenkaoshi:1_huoyuedu:1_chongwuroughun:1_famillyshop:1_juedou:1', // 游戏开关
	errorUrl:'www.baidu.com',   // 错误页面URL
	chargeUrl:'www.baidu.com',  // 充值页面URL
	fcmUrl:'',                  // FCM URL
	kuafucharge:'1',            // 跨服充值
	huigui:'1',                 // 回归
	forbiddenStr:'1',           // 禁止字符串
	hideXianFa:'0',             // 隐藏仙法
	enableDownload:'1',         // 启用下载
	local:'1',                  // 本地
	waisaiCfg:'1',              // 外赛配置
	isTuiJianWaSai:'1',         // 是否推荐外赛
	shituSwitch:'1',            // 师徒开关
	waSaiUrl:'1',               // 外赛URL
	prizeBtnType:'1',           // 奖品按钮类型
	ext:'1',                    // 扩展
	fbapp:'1',                  // Facebook应用
	hideFavMenu:'1',            // 隐藏收藏菜单
  };
  
  // Main.swf版本号（用于文件路径，不是传递给Flash的参数）
  var mainVersion = '2026072201';

  // 如果mainVersion未定义，使用当前时间生成
  if (mainVersion == undefined || mainVersion == '') {
    var now = new Date();
    var y = now.getFullYear();
    var m = now.getMonth()+1;
    var d = now.getDate();
    var h = now.getHours();
    mainVersion = "" + y + (m < 10 ? "0"+m : m) + (d < 10 ? "0"+d : d) + (h < 10 ? "0"+h : h);
  }
  
  // Main.swf文件路径（应该在Web服务器根目录）
  var mainswf = "/act_web_tiyan/Main.swf?" + mainVersion;
  
  // 编码所有参数（Flash会自动解码）
  for (var k in params) {
    if (params.hasOwnProperty(k)) {
      params[k] = encodeURIComponent(params[k]);
    }
  }
 
	//xxxlog(mainswf);
  printObj(params);
  embedFlash(params, mainswf);

</script>
<script type="text/javascript">


   
  function refreshPage() {
	
    setTimeout("doRefreshPage()", 10);
  }
  function doRefreshPage() {
    try { 
      self.parent.location.reload(true); 
    } catch (e) {
    }
  }
  function bookmarkit(){
    var cookie = document.cookie;
    if(!cookie || cookie.indexOf('addFavorite')==-1) {
      addfavorite();    
    }
  }
  function addfavorite() {
    // 注意：favUrl和favTitleUrl参数已删除，因为客户端代码中没有使用
    // 如果需要收藏功能，可以在这里设置默认值
    var s_url = window.location.href;
    var s_title = document.title || "火影世界游戏页面";
    var expDate = new Date(2099,1,1);
    document.cookie='addFavorite=true; expires=' + expDate.toGMTString();
    if(!document.cookie)
      return;
    try {
      window.external.addFavorite(s_url, s_title);
    }
    catch (e) {
      try  {
        window.sidebar.addPanel(s_title, s_url, "");
      }
      catch (e) {
      }
    }
  }
  try{
    window.onbeforeunload =function(){
      return "火影世界游戏页面";
    }
    window.onunload =function(){
      bookmarkit();
    }
	window.onload =function(){
		var fls = flashChecker();
		if(!fls.f){
			var a = document.createElement('a');
			a.setAttribute('href', "https://www.flash.cn/");
			a.setAttribute('target', '_self');
			a.setAttribute('id', 'startTelMedicine');
			// 防止反复添加
			if(document.getElementById('startTelMedicine')) {
				document.body.removeChild(document.getElementById('startTelMedicine'));
			}
			document.body.appendChild(a);
			a.click();
		}
      
    }
  }catch(e){}
</script>
</head>
<body>
<div id="flashContainer">
<div id="flash">
<h1></h1>
<p></p>
</div>
</div>
</body>
</html>


