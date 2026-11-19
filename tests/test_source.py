from tests.support import SourceTest

class CatalogTest(SourceTest):
    def test_cap_overlap_and_scoped_change(self):
        a=self.get('/catalog?unit=east');self.assertEqual(a['total'],9);self.assertTrue(a['truncated'])
        b=self.get('/catalog?unit=east&industry=robot&year=2025');self.assertEqual(b['total'],5);self.assertTrue(b['truncated'])
        self.assertEqual(self.get('/catalog?unit=west')['items'][0]['id'],'0')
        self.control('move',unit='east',id='0')
        self.assertEqual(self.request('/catalog?unit=east&version=1')[0],409)
        self.assertEqual(self.get('/catalog?unit=west&version=1')['version'],1)
        self.assertEqual(self.get('/catalog?unit=east&industry=medical')['items'][0]['id'],'0')
    def test_throttling_and_backdate(self):
        self.control('throttle',unit='east');self.assertEqual(self.request('/catalog?unit=east')[0],429)
        self.assertEqual(self.request('/catalog?unit=west')[0],200)
        self.control('backdate',unit='west');self.assertEqual(self.get('/catalog?unit=west&year=2024')['items'][0]['id'],'older-added')
        self.assertEqual(self.request('/catalog?unit=west&prefix=x')[0],400)
