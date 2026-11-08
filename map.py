from typing import Type, Dict


class Outcome:
    pass

class State: 
    pass



class State1(State):
    class OutcomeA:
        pass

    class OutcomeB:
        pass

class State2(State):
    class OutcomeC:
        pass

    class OutcomeD:
        pass

transitions: Dict[Type[Outcome], Type[State]] = {
    State1.OutcomeA: State2,
    State1.OutcomeB: State2
}