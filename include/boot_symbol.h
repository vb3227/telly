#pragma once
#include <cmath>
#include <cstdint>
#include <initializer_list>

// Display-independent vector mark. Identical in both firmware projects.
template <class DrawLine> void renderBootSymbol(unsigned progress, DrawLine draw) {
    const float fillY = 224.0f - 190.0f * (progress > 100 ? 100 : progress) / 100.0f;
    const uint32_t gray = 0x46505B, green = 0x48E89B;
    auto line = [&](float x1, float y1, float x2, float y2) {
        if ((y1 >= fillY) == (y2 >= fillY)) draw((int)x1,(int)y1,(int)x2,(int)y2,y1>=fillY?green:gray);
        else {
            float x = x1 + (x2-x1)*(fillY-y1)/(y2-y1);
            draw((int)x1,(int)y1,(int)x,(int)fillY,y1>=fillY?green:gray);
            draw((int)x,(int)fillY,(int)x2,(int)y2,y2>=fillY?green:gray);
        }
    };
    constexpr float pi = 3.14159265358979323846f;
    for (int i=0;i<32;++i) {
        float a=2*pi*i/32, tip=83+(i%3)*5;
        float x1=120+64*cosf(a),y1=133+64*sinf(a);
        float x2=120+tip*cosf(a+0.09f),y2=133+tip*sinf(a+0.09f);
        float x3=120+69*cosf(a+0.15f),y3=133+69*sinf(a+0.15f);
        line(x1,y1,x2,y2);line(x2,y2,x3,y3);
        line(x1,y1,120+76*cosf(a+0.015f),133+76*sinf(a+0.015f));
    }
    for(int d=0;d<3;++d){line(120,64+d,52+d,188-d);line(52+d,188-d,188-d,188-d);line(188-d,188-d,120,64+d);}
    line(120,78,66,180);line(66,180,174,180);line(174,180,120,78);
    float previousX=79,previousY=137;
    for(int i=1;i<=40;++i){float t=pi*i/40,x=120-41*cosf(t),y=137-19*sinf(t);line(previousX,previousY,x,y);previousX=x;previousY=y;}
    for(int i=1;i<=40;++i){float t=pi*i/40,x=120+41*cosf(t),y=137+19*sinf(t);line(previousX,previousY,x,y);previousX=x;previousY=y;}
    for(int radius:{7,12,16})for(int i=0;i<48;++i){float a=2*pi*i/48,b=2*pi*(i+1)/48;line(120+radius*cosf(a),137+radius*sinf(a),120+radius*cosf(b),137+radius*sinf(b));}
    for(int i=0;i<14;++i){float a=2*pi*i/14;line(120+17*cosf(a),137+17*sinf(a),120+19*cosf(a+0.04f),137+19*sinf(a+0.04f));}
}
