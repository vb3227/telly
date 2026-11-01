#pragma once
#include <stdint.h>
// B is deliberately unassigned. Only a debounced A press creates an action.
class ButtonControls {
    bool initialized=false,pressed=false;
public:
    enum Action {None,Voice};
    Action update(bool a,bool b,uint32_t now){
        (void)b;(void)now;
        if(!initialized){initialized=true;pressed=a;return None;}
        bool start=a&&!pressed;pressed=a;return start?Voice:None;
    }
};
