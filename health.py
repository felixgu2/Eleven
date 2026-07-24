"""Basic derived health metrics from profile measurements."""
from datetime import date


def bmi_category(bmi):
    if bmi is None:
        return None
    if bmi < 18.5:
        return "Underweight"
    if bmi < 25:
        return "Healthy weight"
    if bmi < 30:
        return "Overweight"
    return "Obese"


def calculate_age(date_of_birth):
    """date_of_birth: a date object or ISO string, or None."""
    if not date_of_birth:
        return None
    if isinstance(date_of_birth, str):
        date_of_birth = date.fromisoformat(date_of_birth)
    today = date.today()
    years = today.year - date_of_birth.year
    if (today.month, today.day) < (date_of_birth.month, date_of_birth.day):
        years -= 1
    return years
