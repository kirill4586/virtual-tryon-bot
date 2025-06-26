from aiogram.fsm.state import State, StatesGroup

class PaymentFSM(StatesGroup):
    waiting_for_fio = State()
