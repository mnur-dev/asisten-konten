import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from core import thumbnail


class Thumbnail9routerTest(unittest.TestCase):
    def test_reference_and_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.png'
            output = Path(tmp) / 'out.png'
            Image.new('RGB', (3, 2), 'red').save(source)
            buf = io.BytesIO()
            Image.new('RGB', (4, 3), 'blue').save(buf, format='PNG')
            class Response:
                status_code = 200
                def json(self):
                    return {'data': [{'b64_json': base64.b64encode(buf.getvalue()).decode()}]}
            with patch.object(thumbnail.requests, 'post', return_value=Response()) as post:
                thumbnail.edit_image(source, 'ubah', output, 'test-key')
            body = post.call_args.kwargs['json']
            self.assertEqual(body['model'], 'cx/gpt-6.1-terra')
            self.assertEqual(base64.b64decode(body['image'].split(',', 1)[1]), source.read_bytes())
            self.assertNotIn('input_references', body)
            with Image.open(output) as image:
                self.assertEqual(image.size, (4, 3))


if __name__ == '__main__':
    unittest.main()
