plugins {
    id("com.android.application")
}

android {
    namespace = "com.cinemaworld.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.cinemaworld.app"
        minSdk = 23
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity:1.10.1")
}
