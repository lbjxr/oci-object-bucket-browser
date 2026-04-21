from app.utils import guess_content_type, is_image_type, is_pdf_type, is_text_type, object_name_from_upload


def test_object_name_from_upload():
    assert object_name_from_upload('/a/b/c.txt') == 'a/b/c.txt'


def test_guess_content_type():
    assert guess_content_type('a.txt').startswith('text/')
    assert is_image_type('image/png')
    assert is_pdf_type('application/pdf')
    assert is_text_type('application/json')
