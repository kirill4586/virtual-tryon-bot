from aiogram.fsm.state import StatesGroup, State

class PaymentFSM(StatesGroup):
    waiting_for_fio = State()