{% extends "base.html" %}

{% block title %}{{ movie.title }} - Tomesh Movies{% endblock %}

{% block content %}

<div class="movie-page">

```
<!-- =====================================================
     TOPBAR
====================================================== -->

<header class="movie-topbar">

    <a href="{{ url_for('home') }}" class="movie-brand">

        <div class="brand-logo">
            TM
            <span>▶</span>
        </div>

        <div class="brand-text">
            <strong>TOMESH</strong>
            <small>MOVIES</small>
        </div>

    </a>

    <a href="{{ url_for('home') }}" class="back-btn">
        <span>←</span>
        Back to Movies
    </a>

</header>


<!-- =====================================================
     HERO
====================================================== -->

<section class="movie-hero">

    <div class="hero-bg-glow"></div>
    <div class="hero-grid"></div>

    <div class="hero-content">

        <div class="now-playing">
            <span>●</span>
            NOW PLAYING
        </div>

        <h1>{{ movie.title }}</h1>

        <div class="title-line"></div>

        <div class="movie-meta">

            {% if movie.category %}
            <span class="category">
                🎬 {{ movie.category }}
            </span>
            {% endif %}

            <span class="views">
                👁 {{ movie.views or 0 }} Views
            </span>

            <span class="quality-badge">
                HD
            </span>

        </div>

        {% if movie.description %}

        <p class="description">
            {{ movie.description }}
        </p>

        {% else %}

        <p class="description">
            अपनी पसंदीदा Movie को आसानी से देखें और Enjoy करें।
            Tomesh Movies पर अपनी cinematic journey शुरू करें।
        </p>

        {% endif %}

        <div class="hero-mini-info">

            <div>
                <span>EXPERIENCE</span>
                <strong>YOUR CINEMA</strong>
            </div>

            <i></i>

            <div>
                <span>STREAM</span>
                <strong>ANYTIME</strong>
            </div>

            <i></i>

            <div>
                <span>PLATFORM</span>
                <strong>TOMESH</strong>
            </div>

        </div>

    </div>


    <!-- =================================================
         POPCORN CARD
    ================================================== -->

    <div class="popcorn-wrapper">

        <div class="glow"></div>

        <div class="orbit orbit-one"></div>
        <div class="orbit orbit-two"></div>

        <div class="popcorn-card">

            <div class="popcorn-top-label">
                <span>TONIGHT</span>
                <b>★</b>
            </div>

            <div class="popcorn-title">
                <span>NOW</span>
                <strong>WATCH</strong>
            </div>

            <div class="popcorn">
                🍿
            </div>

            <strong class="popcorn-main">
                Grab Your Popcorn!
            </strong>

            <span class="popcorn-sub">
                Movie time starts now
            </span>

            <div class="popcorn-dots">
                <i></i>
                <i></i>
                <i></i>
            </div>

        </div>

    </div>

</section>


<!-- =====================================================
     TOP AD
====================================================== -->

{% if ads and ads.ad_top %}

<div class="movie-ad movie-ad-top">
    {{ ads.ad_top | safe }}
</div>

{% endif %}


<!-- =====================================================
     VIDEO PLAYER
====================================================== -->

{% if movie.video_url or movie.video %}

<section class="player-section">

    <div class="player-header">

        <div class="player-heading">

            <span class="play-icon">
                ▶
            </span>

            <div>
                <span class="player-kicker">
                    TOMESH MOVIES
                </span>

                <strong>
                    Watch Now
                </strong>
            </div>

        </div>

        <div class="player-status">

            <span class="live-dot"></span>

            <span id="playerStatusText">
                READY
            </span>

            <b>
                HD
            </b>

        </div>

    </div>


    <div class="video-frame">

        <div class="video-frame-top"></div>

        <div class="video-box">

            <video
                id="tmVideo"
                class="tm-video"
                controls
                playsinline
                preload="metadata"
                poster="{{ movie.poster_url or '' }}"
            >

                {% if movie.video_url %}

                <source
                    src="{{ movie.video_url }}"
                    type="{{ movie.video_mime or 'video/mp4' }}"
                >

                {% elif movie.video %}

                <source
                    src="{{ movie.video }}"
                    type="{{ movie.video_mime or 'video/mp4' }}"
                >

                {% endif %}

                आपका browser video playback support नहीं करता।

            </video>


            <!-- Loading overlay -->

            <div
                id="videoLoading"
                class="video-loading"
            >

                <div class="loading-spinner"></div>

                <span>
                    Video loading...
                </span>

            </div>


            <!-- Error overlay -->

            <div
                id="videoError"
                class="video-error"
                hidden
            >

                <div class="video-error-icon">
                    ⚠
                </div>

                <strong>
                    Video play नहीं हो पाई
                </strong>

                <span>
                    Video को दोबारा load करने के लिए नीचे button दबाएँ।
                </span>

                <button
                    type="button"
                    onclick="reloadVideo()"
                >
                    ↻ Retry
                </button>

            </div>

        </div>

        <div class="video-frame-bottom"></div>

    </div>


    <!-- =================================================
         VIDEO ACTIONS
    ================================================== -->

    <div class="video-actions">

        <button
            type="button"
            class="action-btn"
            onclick="toggleFullscreen()"
        >
            <span>⛶</span>
            Fullscreen
        </button>

        <button
            type="button"
            class="action-btn"
            onclick="restartVideo()"
        >
            <span>↻</span>
            Restart
        </button>

    </div>

</section>

{% endif %}


<!-- =====================================================
     PLAYER AD
====================================================== -->

{% if ads and ads.ad_player %}

<div class="movie-ad movie-ad-player">
    {{ ads.ad_player | safe }}
</div>

{% endif %}


<!-- =====================================================
     MOVIE INFORMATION
====================================================== -->

<section class="movie-details">

    <div class="detail-card">

        <div class="detail-icon">
            🎬
        </div>

        <div class="detail-content">

            <small>MOVIE</small>

            <strong>
                {{ movie.title }}
            </strong>

        </div>

    </div>


    {% if movie.category %}

    <div class="detail-card">

        <div class="detail-icon">
            🏷️
        </div>

        <div class="detail-content">

            <small>CATEGORY</small>

            <strong>
                {{ movie.category }}
            </strong>

        </div>

    </div>

    {% endif %}


    <div class="detail-card">

        <div class="detail-icon">
            👁
        </div>

        <div class="detail-content">

            <small>VIEWS</small>

            <strong>
                {{ movie.views or 0 }}
            </strong>

        </div>

    </div>

</section>


<!-- =====================================================
     BOTTOM AD
====================================================== -->

{% if ads and ads.ad_bottom %}

<div class="movie-ad movie-ad-bottom">
    {{ ads.ad_bottom | safe }}
</div>

{% endif %}


<!-- =====================================================
     FOOTER
====================================================== -->

<footer class="movie-footer">

    <div class="footer-logo">
        TM
    </div>

    <div class="footer-text">

        <strong>
            TOMESH MOVIES
        </strong>

        <span>
            Your Cinematic World
        </span>

    </div>

    <div class="footer-line"></div>

    <span class="footer-copy">
        © Tomesh Movies
    </span>

</footer>
```

</div>

<style>

/* =========================================================
   PAGE
========================================================= */

.movie-page,
.movie-page *{
    box-sizing:border-box;
}

.movie-page{
    min-height:100vh;
    padding:0 25px 55px;
    color:#fff;
    position:relative;
    overflow:hidden;

    background:
        radial-gradient(
            circle at 80% 5%,
            rgba(139,92,246,.12),
            transparent 28%
        ),
        radial-gradient(
            circle at 15% 45%,
            rgba(255,193,7,.045),
            transparent 25%
        ),
        radial-gradient(
            circle at 60% 100%,
            rgba(239,35,60,.045),
            transparent 30%
        ),
        #050507;
}


/* =========================================================
   TOPBAR
========================================================= */

.movie-topbar{
    max-width:1200px;
    height:78px;
    margin:auto;

    display:flex;
    align-items:center;
    justify-content:space-between;

    border-bottom:1px solid rgba(255,255,255,.07);
}

.movie-brand{
    display:flex;
    align-items:center;
    gap:12px;

    text-decoration:none;
    color:#fff;
}

.brand-logo{
    width:45px;
    height:45px;

    position:relative;

    display:flex;
    align-items:center;
    justify-content:center;

    border-radius:13px;

    color:#090a0d;

    background:
        linear-gradient(
            135deg,
            #ffc107,
            #ff8a00,
            #ef233c
        );

    font-size:15px;
    font-weight:1000;

    box-shadow:
        0 10px 30px rgba(255,193,7,.17);

    transition:.3s;
}

.movie-brand:hover .brand-logo{
    transform:rotate(-4deg) scale(1.05);
}

.brand-logo span{
    position:absolute;

    right:-6px;
    bottom:-5px;

    width:21px;
    height:21px;

    display:flex;
    align-items:center;
    justify-content:center;

    border-radius:50%;

    background:#fff;
    color:#111;

    font-size:8px;
}

.brand-text{
    display:flex;
    flex-direction:column;
    line-height:1;
}

.brand-text strong{
    font-size:16px;
    letter-spacing:1.4px;
}

.brand-text small{
    margin-top:5px;
    color:#ffc107;
    font-size:8px;
    letter-spacing:3px;
    font-weight:900;
}

.back-btn{
    padding:9px 14px;

    display:flex;
    align-items:center;
    gap:7px;

    border:1px solid rgba(255,255,255,.09);
    border-radius:10px;

    background:rgba(255,255,255,.025);

    color:#858b97;

    text-decoration:none;

    font-size:10px;
    font-weight:800;

    transition:.25s;
}

.back-btn:hover{
    color:#ffc107;
    border-color:rgba(255,193,7,.25);
    background:rgba(255,193,7,.045);
    transform:translateY(-2px);
}


/* =========================================================
   HERO
========================================================= */

.movie-hero{
    max-width:1150px;
    min-height:445px;

    margin:auto;
    padding:60px 0 45px;

    position:relative;

    display:grid;
    grid-template-columns:minmax(0,1fr) 310px;

    gap:55px;

    align-items:center;
}

.hero-bg-glow{
    position:absolute;

    width:500px;
    height:500px;

    right:70px;
    top:-120px;

    border-radius:50%;

    background:
        radial-gradient(
            circle,
            rgba(139,92,246,.11),
            transparent 68%
        );

    pointer-events:none;

    animation:heroGlow 6s ease-in-out infinite alternate;
}

@keyframes heroGlow{

    from{
        transform:scale(.9);
        opacity:.5;
    }

    to{
        transform:scale(1.1);
        opacity:1;
    }

}

.hero-grid{
    position:absolute;

    left:-100px;
    right:-100px;
    bottom:0;

    height:170px;

    opacity:.08;

    background-image:
        linear-gradient(
            rgba(255,255,255,.16) 1px,
            transparent 1px
        ),
        linear-gradient(
            90deg,
            rgba(255,255,255,.16) 1px,
            transparent 1px
        );

    background-size:35px 35px;

    mask-image:
        linear-gradient(
            to top,
            black,
            transparent
        );

    pointer-events:none;
}

.hero-content{
    min-width:0;
    position:relative;
    z-index:2;

    animation:
        heroContentIn
        .8s
        cubic-bezier(.16,1,.3,1)
        both;
}

@keyframes heroContentIn{

    from{
        opacity:0;
        transform:translateY(22px);
    }

    to{
        opacity:1;
        transform:translateY(0);
    }

}

.now-playing{
    margin-bottom:14px;

    display:flex;
    align-items:center;
    gap:7px;

    color:#ffc107;

    font-size:9px;
    font-weight:1000;

    letter-spacing:3px;
}

.now-playing span{
    color:#ef233c;
    font-size:8px;

    animation:
        nowPulse
        1.5s
        ease-in-out
        infinite;
}

@keyframes nowPulse{

    0%,100%{
        opacity:.4;
    }

    50%{
        opacity:1;
    }

}

.hero-content h1{
    max-width:850px;

    margin:0;

    font-size:
        clamp(
            38px,
            5.5vw,
            70px
        );

    line-height:.98;
    font-weight:1000;
    letter-spacing:-2.5px;

    overflow-wrap:anywhere;

    background:
        linear-gradient(
            100deg,
            #fff 0%,
            #fff 45%,
            #cfd1d7 100%
        );

    -webkit-background-clip:text;
    background-clip:text;
    color:transparent;
}

.title-line{
    width:80px;
    height:3px;

    margin-top:18px;

    border-radius:10px;

    background:
        linear-gradient(
            90deg,
            #ffc107,
            #ef233c
        );
}

.movie-meta{
    margin-top:18px;

    display:flex;
    align-items:center;
    flex-wrap:wrap;
    gap:9px;
}

.category{
    display:inline-flex;
    align-items:center;

    padding:7px 12px;

    border-radius:8px;

    color:#08090b;

    background:
        linear-gradient(
            135deg,
            #ffc107,
            #ff9f00
        );

    font-size:10px;
    font-weight:1000;
}

.views{
    color:#777e8b;
    font-size:11px;
}

.quality-badge{
    padding:5px 7px;

    border:1px solid rgba(255,255,255,.1);
    border-radius:6px;

    color:#aeb3bc;

    background:rgba(255,255,255,.035);

    font-size:8px;
    font-weight:1000;
}

.description{
    max-width:720px;

    margin:19px 0 0;

    color:#8c929d;

    font-size:13px;
    line-height:1.85;
}

.hero-mini-info{
    margin-top:25px;

    display:flex;
    align-items:center;
    gap:15px;
}

.hero-mini-info div{
    display:flex;
    flex-direction:column;
}

.hero-mini-info span{
    color:#555c68;
    font-size:7px;
    font-weight:900;
    letter-spacing:1.5px;
}

.hero-mini-info strong{
    margin-top:4px;
    color:#d7d9de;
    font-size:9px;
    letter-spacing:.5px;
}

.hero-mini-info i{
    width:1px;
    height:28px;
    background:rgba(255,255,255,.1);
}


/* =========================================================
   POPCORN
========================================================= */

.popcorn-wrapper{
    position:relative;
    z-index:2;
}

.glow{
    position:absolute;

    width:230px;
    height:230px;

    left:50%;
    top:50%;

    transform:
        translate(-50%,-50%);

    border-radius:50%;

    background:
        radial-gradient(
            circle,
            rgba(255,193,7,.22),
            rgba(139,92,246,.08),
            transparent 68%
        );

    filter:blur(25px);

    animation:
        popcornGlow
        4s
        ease-in-out
        infinite alternate;
}

@keyframes popcornGlow{

    from{
        transform:
            translate(-50%,-50%)
            scale(.85);
    }

    to{
        transform:
            translate(-50%,-50%)
            scale(1.12);
    }

}

.orbit{
    position:absolute;

    left:50%;
    top:50%;

    border:1px solid rgba(255,193,7,.1);

    border-radius:50%;

    pointer-events:none;
}

.orbit-one{
    width:285px;
    height:285px;

    transform:
        translate(-50%,-50%)
        rotate(20deg);

    border-left-color:rgba(255,193,7,.35);

    animation:
        orbitRotate
        12s
        linear
        infinite;
}

.orbit-two{
    width:245px;
    height:245px;

    transform:
        translate(-50%,-50%)
        rotate(-20deg);

    border-right-color:rgba(139,92,246,.3);

    animation:
        orbitRotateReverse
        9s
        linear
        infinite;
}

@keyframes orbitRotate{

    to{
        transform:
            translate(-50%,-50%)
            rotate(380deg);
    }

}

@keyframes orbitRotateReverse{

    to{
        transform:
            translate(-50%,-50%)
            rotate(-380deg);
    }

}

.popcorn-card{
    position:relative;

    padding:27px 20px 25px;

    text-align:center;

    border:1px solid rgba(255,255,255,.09);
    border-radius:22px;

    background:
        linear-gradient(
            145deg,
            rgba(23,24,31,.97),
            rgba(10,11,15,.97)
        );

    box-shadow:
        0 25px 70px rgba(0,0,0,.42),
        inset 0 1px 0 rgba(255,255,255,.035);

    overflow:hidden;

    backdrop-filter:blur(15px);
}

.popcorn-top-label{
    position:relative;

    display:flex;
    justify-content:space-between;
    align-items:center;

    color:#575e6a;

    font-size:7px;
    font-weight:900;
    letter-spacing:2px;
}

.popcorn-top-label b{
    color:#ffc107;
    font-size:10px;
}

.popcorn-title{
    position:relative;

    margin-top:9px;

    display:flex;
    flex-direction:column;
}

.popcorn-title span{
    color:#ffc107;
    font-size:9px;
    font-weight:1000;
    letter-spacing:4px;
}

.popcorn-title strong{
    margin-top:3px;
    font-size:29px;
    letter-spacing:2px;
}

.popcorn{
    position:relative;

    margin:18px 0;

    font-size:72px;
    line-height:1;

    filter:
        drop-shadow(
            0 12px 18px rgba(0,0,0,.4)
        );

    animation:
        tmFloat
        2.8s
        ease-in-out
        infinite;
}

@keyframes tmFloat{

    0%,100%{
        transform:
            translateY(0)
            rotate(-1deg);
    }

    50%{
        transform:
            translateY(-9px)
            rotate(3deg);
    }

}

.popcorn-main{
    position:relative;
    display:block;
    font-size:14px;
}

.popcorn-sub{
    position:relative;
    display:block;
    margin-top:5px;
    color:#666d79;
    font-size:9px;
}

.popcorn-dots{
    margin-top:16px;

    display:flex;
    justify-content:center;
    gap:5px;
}

.popcorn-dots i{
    width:5px;
    height:5px;

    border-radius:50%;
    background:#393d46;
}

.popcorn-dots i:first-child{
    background:#ffc107;
}


/* =========================================================
   ADS
========================================================= */

.movie-ad{
    max-width:1200px;
    margin:22px auto;
    text-align:center;
    overflow:hidden;
}

.movie-ad-top,
.movie-ad-player,
.movie-ad-bottom{
    min-height:45px;
}


/* =========================================================
   PLAYER
========================================================= */

.player-section{
    max-width:1150px;
    margin:0 auto 30px;
}

.player-header{
    margin-bottom:13px;

    display:flex;
    align-items:center;
    justify-content:space-between;
}

.player-heading{
    display:flex;
    align-items:center;
    gap:10px;
}

.play-icon{
    width:34px;
    height:34px;

    display:grid;
    place-items:center;

    border-radius:10px;

    color:#08090b;

    background:
        linear-gradient(
            135deg,
            #ffc107,
            #ff9f00
        );

    font-size:11px;
}

.player-heading > div{
    display:flex;
    flex-direction:column;
}

.player-kicker{
    color:#555c68;
    font-size:7px;
    font-weight:900;
    letter-spacing:2px;
}

.player-heading strong{
    margin-top:3px;
    font-size:17px;
}

.player-status{
    display:flex;
    align-items:center;
    gap:7px;

    color:#656c78;

    font-size:8px;
    font-weight:900;
    letter-spacing:1px;
}

.live-dot{
    width:6px;
    height:6px;

    border-radius:50%;

    background:#ef233c;

    box-shadow:
        0 0 12px rgba(239,35,60,.7);
}

.player-status b{
    margin-left:5px;

    padding:4px 6px;

    border:1px solid rgba(255,255,255,.1);
    border-radius:5px;

    color:#aaa;

    font-size:7px;
}


/* =========================================================
   VIDEO FRAME
========================================================= */

.video-frame{
    position:relative;

    padding:1px;

    border-radius:20px;

    background:
        linear-gradient(
            135deg,
            rgba(255,193,7,.35),
            rgba(255,255,255,.07) 30%,
            rgba(139,92,246,.25) 70%,
            rgba(255,193,7,.2)
        );

    box-shadow:
        0 30px 80px rgba(0,0,0,.5);
}

.video-frame-top,
.video-frame-bottom{
    position:absolute;

    left:20px;
    right:20px;

    height:1px;

    z-index:5;

    background:
        linear-gradient(
            90deg,
            transparent,
            rgba(255,193,7,.55),
            transparent
        );

    pointer-events:none;
}

.video-frame-top{
    top:0;
}

.video-frame-bottom{
    bottom:0;
}

.video-box{
    position:relative;

    width:100%;

    background:#000;

    border-radius:19px;

    overflow:hidden;
}

.tm-video{
    display:block;

    width:100%;

    min-height:300px;
    max-height:700px;

    background:#000;

    object-fit:contain;
}


/* =========================================================
   VIDEO LOADING
========================================================= */

.video-loading{
    position:absolute;

    inset:0;

    z-index:10;

    display:flex;

    flex-direction:column;

    align-items:center;
    justify-content:center;

    gap:12px;

    background:
        rgba(0,0,0,.72);

    backdrop-filter:blur(3px);

    color:#aaa;

    font-size:11px;

    pointer-events:none;

    transition:
        opacity .25s,
        visibility .25s;
}

.video-loading.hidden{
    opacity:0;
    visibility:hidden;
}

.loading-spinner{
    width:38px;
    height:38px;

    border-radius:50%;

    border:3px solid rgba(255,255,255,.12);

    border-top-color:#ffc107;

    animation:
        videoSpin
        .8s
        linear
        infinite;
}

@keyframes videoSpin{

    to{
        transform:rotate(360deg);
    }

}


/* =========================================================
   VIDEO ERROR
========================================================= */

.video-error{
    position:absolute;

    inset:0;

    z-index:11;

    display:flex;

    flex-direction:column;

    align-items:center;
    justify-content:center;

    gap:9px;

    padding:20px;

    text-align:center;

    background:
        rgba(0,0,0,.88);

    color:#fff;
}

.video-error[hidden]{
    display:none;
}

.video-error-icon{
    width:48px;
    height:48px;

    display:grid;
    place-items:center;

    border-radius:50%;

    color:#fff;

    background:
        rgba(239,35,60,.15);

    border:1px solid rgba(239,35,60,.3);

    font-size:22px;
}

.video-error strong{
    font-size:14px;
}

.video-error span{
    max-width:330px;

    color:#777e8b;

    font-size:10px;

    line-height:1.6;
}

.video-error button{
    margin-top:5px;

    padding:9px 16px;

    border:0;

    border-radius:8px;

    background:
        linear-gradient(
            135deg,
            #ffc107,
            #ff9f00
        );

    color:#111;

    cursor:pointer;

    font-size:10px;
    font-weight:900;
}


/* =========================================================
   VIDEO ACTIONS
========================================================= */

.video-actions{
    margin-top:12px;

    display:flex;
    justify-content:flex-end;

    gap:8px;
}

.action-btn{
    min-height:34px;

    padding:0 13px;

    display:flex;
    align-items:center;
    gap:7px;

    border:1px solid rgba(255,255,255,.08);
    border-radius:9px;

    background:#101218;

    color:#858c98;

    cursor:pointer;

    font-size:9px;
    font-weight:800;

    transition:.25s;
}

.action-btn span{
    color:#ffc107;
    font-size:13px;
}

.action-btn:hover{
    color:#fff;

    border-color:rgba(255,193,7,.25);

    background:
        linear-gradient(
            135deg,
            rgba(255,193,7,.1),
            rgba(139,92,246,.06)
        );

    transform:translateY(-2px);
}


/* =========================================================
   DETAILS
========================================================= */

.movie-details{
    max-width:1150px;

    margin:27px auto;

    display:grid;

    grid-template-columns:
        repeat(3,1fr);

    gap:12px;
}

.detail-card{
    min-width:0;

    display:flex;
    align-items:center;

    gap:12px;

    padding:16px;

    border:1px solid rgba(255,255,255,.065);
    border-radius:15px;

    background:
        linear-gradient(
            145deg,
            #101218,
            #090b0f
        );

    transition:.25s;
}

.detail-card:hover{
    transform:translateY(-4px);

    border-color:rgba(255,193,7,.2);

    box-shadow:
        0 15px 35px rgba(0,0,0,.3);
}

.detail-icon{
    width:43px;
    height:43px;

    flex-shrink:0;

    display:grid;
    place-items:center;

    border-radius:11px;

    background:
        linear-gradient(
            135deg,
            rgba(255,193,7,.09),
            rgba(139,92,246,.07)
        );

    font-size:19px;
}

.detail-content{
    min-width:0;

    display:flex;
    flex-direction:column;

    gap:5px;
}

.detail-content small{
    color:#555c68;

    font-size:7px;
    font-weight:900;

    letter-spacing:1.5px;
}

.detail-content strong{
    overflow:hidden;

    text-overflow:ellipsis;

    white-space:nowrap;

    color:#e1e3e7;

    font-size:12px;
}


/* =========================================================
   FOOTER
========================================================= */

.movie-footer{
    max-width:1150px;

    margin:50px auto 0;

    padding-top:22px;

    display:flex;
    align-items:center;

    gap:12px;

    border-top:1px solid rgba(255,255,255,.065);
}

.footer-logo{
    width:38px;
    height:38px;

    display:grid;
    place-items:center;

    border-radius:10px;

    color:#08090b;

    background:
        linear-gradient(
            135deg,
            #ffc107,
            #ff9f00
        );

    font-size:11px;
    font-weight:1000;
}

.footer-text{
    display:flex;
    flex-direction:column;

    gap:4px;
}

.footer-text strong{
    color:#d8dbe1;

    font-size:10px;

    letter-spacing:1px;
}

.footer-text span{
    color:#555c68;
    font-size:8px;
}

.footer-line{
    flex:1;
    height:1px;

    background:
        linear-gradient(
            90deg,
            rgba(255,255,255,.08),
            transparent
        );
}

.footer-copy{
    color:#3f454f;
    font-size:8px;
}


/* =========================================================
   TABLET
========================================================= */

@media(max-width:900px){

    .movie-page{
        padding-left:16px;
        padding-right:16px;
    }

    .movie-hero{
        grid-template-columns:1fr;
        gap:40px;
        padding-top:45px;
    }

    .popcorn-wrapper{
        width:330px;
        max-width:100%;
        margin:auto;
    }

}


/* =========================================================
   MOBILE
========================================================= */

@media(max-width:600px){

    .movie-page{
        padding:
            0 12px 35px;
    }

    .movie-topbar{
        height:62px;
    }

    .brand-logo{
        width:38px;
        height:38px;
        border-radius:10px;
    }

    .brand-text strong{
        font-size:14px;
    }

    .brand-text small{
        font-size:7px;
    }

    .back-btn{
        padding:7px 10px;
        font-size:9px;
    }

    .movie-hero{
        min-height:auto;

        padding:
            32px 0 30px;

        gap:30px;
    }

    .hero-content h1{
        font-size:32px;
        letter-spacing:-1.3px;
    }

    .description{
        font-size:12px;
        line-height:1.75;
    }

    .hero-mini-info{
        gap:9px;
    }

    .hero-mini-info strong{
        font-size:8px;
    }

    .hero-mini-info span{
        font-size:6px;
    }

    .popcorn-wrapper{
        width:100%;
        max-width:320px;
    }

    .popcorn-card{
        padding:
            22px 15px;
    }

    .popcorn{
        font-size:63px;
    }

    .orbit-one{
        width:265px;
        height:265px;
    }

    .orbit-two{
        width:225px;
        height:225px;
    }

    .player-header{
        align-items:flex-end;
    }

    .player-heading strong{
        font-size:15px;
    }

    .player-status{
        font-size:7px;
    }

    .video-frame{
        border-radius:12px;
    }

    .video-box{
        border-radius:11px;
    }

    .tm-video{
        min-height:210px;
        max-height:450px;
    }

    .video-actions{
        justify-content:stretch;
    }

    .action-btn{
        flex:1;
        justify-content:center;
    }

    .movie-details{
        grid-template-columns:1fr;
        gap:9px;
    }

    .detail-card{
        padding:13px;
    }

    .movie-footer{
        margin-top:35px;
    }

    .footer-copy{
        display:none;
    }

}


/* =========================================================
   SMALL MOBILE
========================================================= */

@media(max-width:380px){

    .brand-text{
        display:none;
    }

    .hero-content h1{
        font-size:28px;
    }

    .hero-mini-info{
        gap:7px;
    }

    .hero-mini-info strong{
        font-size:7px;
    }

    .hero-mini-info i{
        height:22px;
    }

    .tm-video{
        min-height:190px;
    }

}


/* =========================================================
   REDUCED MOTION
========================================================= */

@media(prefers-reduced-motion:reduce){

    *,
    *::before,
    *::after{
        animation-duration:.01ms !important;
        animation-iteration-count:1 !important;
        transition-duration:.01ms !important;
    }

}

</style>

<script>

/* =========================================================
   TOMESH MOVIES VIDEO PLAYER
========================================================= */

document.addEventListener(
    "DOMContentLoaded",
    function(){

        const video =
            document.getElementById("tmVideo");

        const loading =
            document.getElementById("videoLoading");

        const errorBox =
            document.getElementById("videoError");

        const status =
            document.getElementById("playerStatusText");


        if(!video){
            return;
        }


        /* -------------------------------------------------
           Helpers
        ------------------------------------------------- */

        function showLoading(){

            if(loading){
                loading.classList.remove("hidden");
            }

            if(errorBox){
                errorBox.hidden = true;
            }

            if(status){
                status.textContent = "LOADING";
            }

        }


        function hideLoading(){

            if(loading){
                loading.classList.add("hidden");
            }

        }


        function showReady(){

            hideLoading();

            if(errorBox){
                errorBox.hidden = true;
            }

            if(status){
                status.textContent = "READY";
            }

        }


        function showPlaying(){

            hideLoading();

            if(errorBox){
                errorBox.hidden = true;
            }

            if(status){
                status.textContent = "PLAYING";
            }

        }


        function showError(){

            hideLoading();

            if(errorBox){
                errorBox.hidden = false;
            }

            if(status){
                status.textContent = "ERROR";
            }

        }


        /* -------------------------------------------------
           Initial state
        ------------------------------------------------- */

        showLoading();


        /* -------------------------------------------------
           Video events
        ------------------------------------------------- */

        video.addEventListener(
            "loadstart",
            function(){

                showLoading();

            }
        );


        video.addEventListener(
            "loadedmetadata",
            function(){

                showReady();

                console.log(
                    "Tomesh Movies: video metadata loaded"
                );

                console.log(
                    "Video duration:",
                    video.duration
                );

            }
        );


        video.addEventListener(
            "canplay",
            function(){

                showReady();

            }
        );


        video.addEventListener(
            "playing",
            function(){

                showPlaying();

            }
        );


        video.addEventListener(
            "pause",
            function(){

                if(
                    !video.ended
                    && !video.error
                ){

                    if(status){
                        status.textContent = "PAUSED";
                    }

                }

            }
        );


        video.addEventListener(
            "waiting",
            function(){

                if(status){
                    status.textContent = "BUFFERING";
                }

            }
        );


        video.addEventListener(
            "ended",
            function(){

                if(status){
                    status.textContent = "ENDED";
                }

            }
        );


        video.addEventListener(
            "error",
            function(){

                console.log(
                    "Tomesh Movies: video playback error",
                    video.error
                );

                showError();

            }
        );


        /* -------------------------------------------------
           Retry automatically once if metadata fails
        ------------------------------------------------- */

        let retryDone = false;

        video.addEventListener(
            "stalled",
            function(){

                console.log(
                    "Tomesh Movies: video stalled"
                );

            }
        );


        video.addEventListener(
            "abort",
            function(){

                console.log(
                    "Tomesh Movies: video request aborted"
                );

            }
        );


        /* -------------------------------------------------
           Source check
        ------------------------------------------------- */

        const source =
            video.querySelector("source");

        if(!source){

            showError();

            return;
        }


        console.log(
            "Tomesh Movies video URL:",
            source.src
        );

        console.log(
            "Tomesh Movies video MIME:",
            source.type
        );


        /* -------------------------------------------------
           Load video
        ------------------------------------------------- */

        try{

            video.load();

        }catch(error){

            console.error(
                "Tomesh Movies video load error:",
                error
            );

            showError();

        }


        /* -------------------------------------------------
           Retry function
        ------------------------------------------------- */

        window.reloadVideo = function(){

            if(!video){
                return;
            }

            retryDone = true;

            showLoading();

            try{

                video.pause();

            }catch(e){}


            try{

                video.load();

            }catch(e){

                console.error(
                    "Video reload error:",
                    e
                );

                showError();

            }

        };

    }
);


/* =========================================================
   FULLSCREEN
========================================================= */

function toggleFullscreen(){

    const video =
        document.getElementById("tmVideo");

    if(!video){
        return;
    }


    if(document.fullscreenElement){

        document.exitFullscreen();

        return;
    }


    if(video.requestFullscreen){

        video.requestFullscreen();

    }else if(video.webkitEnterFullscreen){

        video.webkitEnterFullscreen();

    }

}


/* =========================================================
   RESTART
========================================================= */

function restartVideo(){

    const video =
        document.getElementById("tmVideo");

    if(!video){
        return;
    }


    try{

        video.currentTime = 0;

    }catch(e){}


    video.play().catch(
        function(error){

            console.log(
                "Tomesh Movies autoplay/play blocked:",
                error
            );

        }
    );

}

</script>

{% endblock %}
